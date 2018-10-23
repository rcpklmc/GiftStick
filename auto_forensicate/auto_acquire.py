# -*- coding: utf-8 -*-
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Automated forensics acquisition script."""

from __future__ import unicode_literals

import argparse
import json
import logging
import sys
from auto_forensicate import errors
from auto_forensicate import uploader
from auto_forensicate.recipes import disk
from auto_forensicate.recipes import firmware
from auto_forensicate.recipes import sysinfo
from auto_forensicate.stamp import manager

import gcs_oauth2_boto_plugin  # pylint: disable=unused-import
import progressbar
from google.cloud import logging as google_logging
from google.cloud.logging.handlers import CloudLoggingHandler
from google.cloud.logging.handlers import setup_logging as setup_gcp_logging
from google.oauth2 import service_account

VALID_RECIPES = {
    'disk': disk.DiskRecipe,
    'firmware': firmware.ChipsecRecipe,
    'sysinfo': sysinfo.SysinfoRecipe
}


class BaBar(progressbar.ProgressBar):
  """A ProgressBar object with an extra update method.

    This is required because the boto library's callback expects a function that
    takes two arguments, and ProgressBar only one.
  """

  def update_with_total(self, current_bytes, total_bytes):
    try:
      self.update(current_bytes)
    except ValueError:
      # This is raised when current_bytes > self.maxval, which happens when we
      # didn't have the correct size of an Artifact at its initialization,
      # ie: all ProcessOutputArtifacts
      self.maxval = current_bytes
      self.update(current_bytes)


class AutoForensicate(object):
  """Class implementing forensics acquisition logic.

    Attributes:
      _recipes (dict[str, BaseRecipe]): the list of valid recipes.
  """

  def __init__(self, recipes=None):
    """Instantiates the AutoForensicate object.

    Args:
      recipes (dict[str, BaseRecipe]): the dict listing available recipes.
    Raises:
      errors.BadConfigOption: if no available recipes dict is passed.
    """
    if recipes is None:
      raise errors.BadConfigOption('The recipes argument must not be None')

    self._errors = []
    self._gcs_settings = None
    self._recipes = recipes
    self._uploader = None
    self._should_retry = False  # True when a recoverable error occurred.
    self._stackdriver_handler = None  # Stackdriver backed logging handler.

  def _CreateParser(self):
    """Returns an instance of argparse.ArgumentParser."""
    parser = argparse.ArgumentParser(
        description='Autopush forensics evidence to Cloud Storage')
    parser.add_argument(
        '--acquire', action='append', help='Evidence to acquire',
        choices=['all']+self._recipes.keys(), required=True
    )
    parser.add_argument(
        'destination', action='store',
        help=(
            'Sets the destination for uploads. '
            'For example gs://bucket_name/path will upload to GCS in bucket '
            '<bucket_name> in the folder </path/>')
    )
    parser.add_argument(
        '--gs_keyfile', action='store', required=False,
        help=(
            'Path to the service account private key JSON file for Google '
            'Cloud')
    )
    parser.add_argument(
        '--logging', action='append', required=False,
        choices=['stackdriver', 'stdout'], default=['stdout'],
        help='Selects logging methods.'
    )
    parser.add_argument(
        '--select_disks', action='store_true', required=False, default=False,
        help='Asks the user to select which disk to acquire'
    )
    return parser

  def _ParseLoggingArguments(self, options):
    """Parses the --logging flag.

    Args:
      options (argparse.Namespace): the parsed command-line arguments.
    Raises:
      errors.BadConfigOption: if the options are invalid.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    self._logger = logging.getLogger(self.__class__.__name__)

    if 'stackdriver' in options.logging:
      if not self._gcs_settings:
        raise errors.BadConfigOption(
            'Please provide a valid --gs_keyfile to enable StackDriver '
            'logging')
      gcp_credentials = service_account.Credentials.from_service_account_file(
          options.gs_keyfile)
      project_id = self._gcs_settings.get('project_id', None)

      gcp_client_logger = google_logging.Client(
          project=project_id, credentials=gcp_credentials)
      self._stackdriver_handler = CloudLoggingHandler(
          gcp_client_logger, name='GiftStick')
      setup_gcp_logging(self._stackdriver_handler)

  def _MakeUploader(self, options):
    """Creates a new Uploader object.

    This instantiates the proper Uploader object to handle the destination URL
    argument.

    Args:
      options (argparse.Namespace): the parsed command-line arguments.
    Returns:
      Uploader: an uploader object.
    Raises:
      errors.BadConfigOption: if the options are invalid.
    """

    stamp_manager = manager.StampManager()

    if options.destination.startswith('gs://'):
      if not self._gcs_settings:
        raise errors.BadConfigOption(
            'Please provide a valid GCS json file. '
            'See --gs_keyfile option'
        )

      client_id = self._gcs_settings.get('client_id', None)

      if not client_id:
        raise errors.BadConfigOption(
            'The provided GCS json file lacks a "client_id" key.'
        )

      return uploader.GCSUploader(
          options.destination, options.gs_keyfile, client_id, stamp_manager)

    return

  def ParseArguments(self, args):
    """Parses the arguments.

    Args:
      args (list): list of arguments.
    Returns:
      argparse.Namespace: parsed command line arguments.
    """
    parser = self._CreateParser()
    options = parser.parse_args(args)

    self._ParseRecipes(options)
    self._gcs_settings = self._ParseGCSJSON(options)
    self._ParseLoggingArguments(options)

    if options.select_disks and 'disk' not in options.acquire:
      raise errors.BadConfigOption(
          '--select_disks needs the disk recipe (current recipes : {0})'.format(
              ', '.join(options.acquire))
      )

    return options

  def _ParseRecipes(self, options):
    if 'all' in options.acquire:
      options.acquire = self._recipes.keys()
    else:
      # Deduplicate recipes
      options.acquire = list(set(options.acquire))

  def _ParseGCSJSON(self, options):
    """Parses a GCS json configuration file.

    Args:
      options (argparse.Namespace): the parsed command-line arguments.
    Returns:
      dict: the dict representation of the JSON object in the config file.
    """
    if options.gs_keyfile:
      with open(options.gs_keyfile, 'r') as json_file_descriptor:
        return json.load(json_file_descriptor)
    return None

  def _MakeProgressBar(self, max_size, name, message=None):
    """Returns a ProgressBar object with default widgets.

    Args:
      max_size (int): the size of the source.
      name (str): the name of what is being processed.
      message (str): an extra message to display before the bar.

    Returns:
      ProgressBar: the progress bar object.
    """
    if message:
      self._logger.info(message)
    if max_size > 0:
      pb = BaBar(
          maxval=max_size, widgets=[
              name, progressbar.Percentage(), ' ',
              progressbar.Bar('=', '[', ']'), ' ', progressbar.ETA(),
              progressbar.FileTransferSpeed()])
    else:
      pb = BaBar(maxval=0, widgets=[name, progressbar.AnimatedMarker()])
    return pb

  def Do(self, recipe):
    """Runs a recipe.

    Args:
      recipe (BaseRecipe): a recipe object.
    """
    with recipe:
      artifacts = recipe.GetArtifacts()
      self._UploadArtifacts(artifacts)

  def _UploadArtifact(self, artifact, update_callback=None):
    """Uploads one Artifact to a remote storage.

    Args:
      artifact (BaseArtifact): the artifact representing the file to upload.
      update_callback (func): the function called with the arguments:
        number_bytes_uploaded, number_bytes_total
    Returns:
      str: the url in the remote storage for the uploaded item.
    """
    try:
      remote_path = self._uploader.UploadArtifact(
          artifact, update_callback=update_callback)
      self._logger.info('Uploaded \'%s\'', remote_path)
    except Exception as e:   # pylint: disable=broad-except
      # We need to catch all Exceptions here, as even if one artifact failed to
      # acquire, we want to try uploading others.
      self._logger.exception('Unable to upload artifact %s', artifact.name)
      self._errors.append(e)

  def _UploadArtifacts(self, artifacts):
    """Uploads a list of Artifacts to a remote storage.

    Args:
      artifacts (list[BaseArtifact]): the list of artifacts to upload.
    """
    nb_tasks = len(artifacts)
    current_task = 0
    for artifact in artifacts:
      current_task += 1
      bar = self._MakeProgressBar(
          artifact.size, artifact.name,
          'Uploading \'{0:s}\' ({1:s}, Task {2:d}/{3:d})'.format(
              artifact.name, artifact.readable_size, current_task, nb_tasks))
      bar.start()
      self._UploadArtifact(artifact, update_callback=bar.update_with_total)
      bar.finish()

  def _Colorize(self, color, msg):
    """Adds a ANSI color to a message.

    Args:
      color(int): The ANSI color escape code.
      msg(str): The message to display.
    Returns:
      str: The colored message.
    """
    reset_color_seq = '\033[0m'
    color_seq = '\033[3{0:d}m'.format(color)

    return color_seq + msg + reset_color_seq

  def Main(self, args=None):
    """Main method for AutoForensicate.

    Args:
      args (list[str]): list of command line arguments.
    Raises:
      Exception: if no Uploader object have been instantiated.
    """
    options = self.ParseArguments(args)

    self._uploader = self._MakeUploader(options)

    if not self._uploader:
      raise Exception('Could not instantiate uploader')

    message = 'Acquisition starting with args \'{0:s}\''.format(sys.argv)
    self._logger.info(message)
    for recipe_name in options.acquire:
      recipe_class = self._recipes.get(recipe_name, None)
      if recipe_class:
        try:
          self.Do(recipe_class(recipe_name, options=options))
        except Exception as e:  # pylint: disable=broad-except
          # We log the error but want to keep acquiring other recipes.
          self._logger.exception('Recipe %s failed to run', recipe_name)
          self._errors.append(e)

    self._logger.info('Acquisition has ended')

    if self._stackdriver_handler:
      # Make sure all logs are sent to StackDriver
      self._stackdriver_handler.transport.worker.stop()
      logging.getLogger().removeHandler(self._stackdriver_handler)

    # The next messages are for the current user only
    red_color_code = 1
    green_color_code = 2

    if not self._errors:
      print self._Colorize(
          green_color_code,
          ('Everything has completed successfully, feel free to shut the system'
           ' down.')
      )
      return

    should_retry = False
    # Error management from down here
    for e in self._errors:
      if isinstance(e, errors.RetryableError):
        should_retry = True

    if should_retry:
      print self._Colorize(
          red_color_code,
          'There was a problem with the upload, please re-run the script.')
    else:
      print self._Colorize(
          red_color_code,
          ('There was a problem with the upload, please keep the system '
           'running and contact the security person who told you to do the '
           'GiftStick process')
      )


if __name__ == '__main__':
  app = AutoForensicate(VALID_RECIPES)
  app.Main(args=sys.argv[1:])
