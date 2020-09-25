from parameters import *

from pygstc.gstc import *
from pygstc.logger import *
import logbook
from logbook.queues import MultiProcessingHandler, MultiProcessingSubscriber

import time
import datetime
from traceback import print_exc
import subprocess
import multiprocessing
import os
import warnings
from collections import OrderedDict


class PipelineEntity(object):
    """

    """
    def __init__(self, client, name, description):
        self._name = name
        self._description = description
        self._client = client
        print("Creating pipeline {} with description {}.".format(self._name, self._description))
        self._client.pipeline_create(self._name, self._description)
    
    def get_name(self):
        return self._name

    def play(self):
        print("Playing pipeline: " + self._name)
        self._client.pipeline_play(self._name)
    
    def stop(self):
        print("Stopping pipeline: " + self._name)
        self._client.pipeline_stop(self._name)
    
    def delete(self):
        print("Deleting pipeline: " + self._name)
        self._client.pipeline_delete(self._name)
    
    def eos(self):
        print("Sending EOS to pipeline: " + self._name)
        self._client.event_eos(self._name)
    
    def set_property(self, element_name, property_name, property_value):
        print("Setting {} property to {}; element {} inside pipeline {}".format(
            property_name, property_value, element_name, self._name))
        self._client.element_set(self._name, element_name, property_name, property_value)
    
    def listen_to(self, sink):
        print(self._name + " pipeline listening to " + sink)
        # interpipesrc element is named according to parameters.PIPE_SOURCE_NAME_FORMATTER
        self._client.element_set(self._name, PIPE_SOURCE_NAME_FORMATTER.format(self._name), 'listen-to', sink)


class GstdManager:
    """
    Manager class for starting and stopping GStreamer Daemon.
    """
    def __init__(self, gst_log=None, gstd_log=None, force_mkdir=False, gst_debug_level=5, 
                 tcp_enable=True, tcp_address='127.0.0.1', tcp_port=5000, num_tcp_ports=1,
                 http_enable=False, http_address='127.0.0.1', http_port=5001):
        # check input arguments
        if gst_log is not None:
            print("> GStreamer log file: {}".format(gst_log))
            logdir = os.path.split(gst_log)[0]
            if not os.path.exists(logdir):
                if force_mkdir is True:
                    print(">> Directory does not exist. Creating {}")
                    os.mkdir(logdir)
                else:
                    raise OSError("'gst_log' directory does not exist. Create or force with `force_mkdir`.")
        if gstd_log is not None:
            print("> GStreamer Daemon log file: {}".format(gstd_log))
            logdir = os.path.split(gst_log)[0]
            if not os.path.exists(logdir):
                if force_mkdir is True:
                    print(">> Directory does not exist. Creating {}")
                    os.mkdir(logdir)
                else:
                    raise OSError("'gstd_log' directory does not exist. Create or force with `force_mkdir`.")
        if type(gst_debug_level) is not int or gst_debug_level > 9 or gst_debug_level < 0:
            raise AttributeError("Provide integer [0, 9] for `gst_debug_level`.")
        # assemble arguments
        self.gstd_args = ['gstd']
        if gst_log is not None:
            self.gstd_args += ['--gst-log-filename', gst_log]
        if gstd_log is not None:
            self.gstd_args += ['--gstd-log-filename', gstd_log]
        self.gstd_args += ['--gst-debug-level', str(gst_debug_level)]
        if tcp_enable is True:
            self.gstd_args += ['--enable-tcp-protocol', '--tcp-address', tcp_address, 
                               '--tcp-base-port', str(tcp_port), '--tcp-num-ports', str(num_tcp_ports)]
        if http_enable is True:
            self.gstd_args += ['--enable-http-protocol', '--http-address', http_address, '--http-port', str(http_port)]
        print("Ready to start GStreamer Daemon.\nShell: {}".format(self.gstd_args))

    def start(self, restart=True):
        self.stop()
        print("Starting GStreamer Daemon...")
        gstd_proc = subprocess.run(self.gstd_args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        if gstd_proc.returncode == 0:
            print("Success.")
        elif gstd_proc.returncode == 1:
            print("Error starting GStreamer Daemon with command {}".format(self.gstd_args))
            if restart is False:
                print("Gstreamer Daemon may already be running. Consider stopping or setting `restart=True`.")
    def stop(self):
        gstd_stop = subprocess.run(['gstd', '--kill'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if 'no running gstd found' in gstd_stop.stderr.lower():
            print("No running GStreamer Daemon for STOP command.")


class IngestSession:
    """
    Manager class for video ingestion. This should run continuously, with triggers setting up and executing various
        components/functions. Capabilities are:
    -
    """
    def __init__(self, session_root_directory, session_config_file):
        """

        :param session_root_directory:
        :param session_config_file:
        :return: None
        """


        # determine session number and root directory
        self.this_session_number = self._next_session_number(session_root_directory)
        session_relative_directory = DEFAULT_SESSION_DIRECTORY_FORMAT.format(self.this_session_number)
        self.session_absolute_directory = os.path.join(session_root_directory, session_relative_directory)
        # one last check for directory consistency
        if DEFAULT_SESSION_DIRECTORY_FORMAT.format(self.this_session_number) in os.listdir(session_root_directory):
            raise FileExistsError("""Directory overwrite conflict! 
            Check parameters.SESSION_DIRECTORY_FORMAT and self._next_session_number().""")
        os.mkdir(self.session_absolute_directory)
        self.session_log_directory = os.path.join(self.session_absolute_directory, 'logs')
        os.mkdir(self.session_log_directory)

        # set up logging that will be used by all processes
        # multiprocessing.set_start_method('spawn')
        self.logqueue = multiprocessing.Queue(-1) # TODO: what is -1?
        self.handler, self.sub = None, None     # initialize these to None; they'll be set in self._setup_logging()
        self._setup_logging()
        logbook.notice("Next session number according to session root directory: {}".format(self.this_session_number))
        logbook.notice("Session directory created: {}".format(self.session_absolute_directory))
        logbook.notice("Session logging directory created: {}".format(self.session_log_directory))
        # fill configuration variables
        # self.camera_config is a list of dictionaries; others are just a dictionary
        # camera configuration retains the order in which the cameras were listed in the config file
        logbook.notice("Parsing configuration file.")
        self.camera_config, self.image_snap_config, self.video_snap_config, self.recording_config = \
            self._parse_config_file(session_config_file)
        config_copy_file = self._copy_config_file(session_config_file)
        logbook.notice("Copying configuration file to {}".format(config_copy_file))
        # write the session header file, which includes derivative configuration information
        header_file = self._write_session_header_file()
        logbook.notice("Wrote session header/info file to {}".format(header_file))
        # instantiate GstD manager to run GStreamer Daemon in the background
        logbook.notice("Initializing GStreamer Daemon manager.")
        self.manager = None
        self.initialize_gstd()
        # instantiate the GstD Python connection client
        logbook.notice("Initializing GStreamer Daemon Python client.")
        self.client = None
        self.initialize_gstd_client()

        # locations to store pipelines {pipeline_name: PipelineEntity, ...}
        # pre-define names for certain pipelines that will be references later for control
        # camera pipelines are assumed to be named the same as the specified camera name
        self.pipelines_cameras = OrderedDict()
        self.image_encoder_name = 'image_encode'
        self.pipelines_video_enc = {}
        self.pipelines_video_buffer = {}
        self.persistent_record_name = 'record_h264'
        self.pipelines_video_rec = {}
        self.video_snap_name = 'snap_video'
        self.image_snap_name = 'snap_image'
        self.pipelines_snap = {}

    def _setup_logging(self, level=logbook.DEBUG):
        self.handler = MultiProcessingHandler(self.logqueue)
        self.handler.push_application()
        target_handlers = logbook.NestedSetup([
            logbook.NullHandler(),
            logbook.StderrHandler(level=logbook.INFO,
                                  format_string='{record.time:%Y-%m-%d %H:%M:%S}|{record.level_name}|{record.message}'),
            logbook.TimedRotatingFileHandler(filename=os.path.join(self.session_log_directory, 'manager.log'),
                                             level=level, bubble=True,
                                             date_format='%Y-%m-%d', timed_filename_for_current=True,
                                             backup_count=5, rollover_format='{basename}-{timestamp}{ext}')
        ])
        self.sub = MultiProcessingSubscriber(self.logqueue)
        self.sub.dispatch_in_background(target_handlers)
        logbook.notice("Logger setup complete")

    def _next_session_number(self, session_root_directory):
        """
        Checks what directories present in `session_root_directory` match the SESSION_DIRECTORY_FORMAT and calculates
            the next session number. Does not wrap around to zero.
        :param session_root_directory: Root directory where session-specific directories will be placed.
        :return: largest session number found in `session_root_directory` plus 1
        """
        root_list_dir = os.listdir(session_root_directory)
        present_matches = [0]
        for i in range(0, 99999):
            if DEFAULT_SESSION_DIRECTORY_FORMAT.format(i) in root_list_dir:
                present_matches.append(i)
        return max(present_matches) + 1

    def _parse_config_file(self, config_file):
        """
        Reads and parses configuration file into config blocks and key/value pairs. Each config return is of the form
            { key: value, key: value, ... }, where all keys and values are strings (types handled downstream).
        :param config_file:
        :return: camera_config, image_snap_config, video_snap_config, recording_config
        """
        camera_config = []
        image_snap_config = []
        video_snap_config = []
        recording_config = []
        block_mapping = {'__CAMERA__': camera_config,
                         '__IMAGE-SNAPSHOT__': image_snap_config,
                         '__VIDEO-SNAPSHOT__': video_snap_config,
                         '__PERSISTENT-RECORDING__': recording_config}
        # open configuration file and parse it out
        with open(config_file, 'r') as f:
            current_block = None
            block_destination = None
            for line in f:
                # ignore empty lines and comment lines
                if line is None or len(line.strip()) == 0 or line[0] == '#':
                    continue
                strip_line = line.strip()
                if len(strip_line) > 2 and strip_line[:2] == '__' and strip_line[-2:] == '__':
                    # this is a configuration block line
                    # first check if this is the first one or not
                    if block_destination is not None and len(current_block) > 0:
                        # add the block to its destination if it's non-empty
                        block_destination.append(current_block)
                    # reset current block to empty and set its destination
                    current_block = {}
                    block_destination = block_mapping[strip_line]
                elif '=' in strip_line:
                    pkey, pval = strip_line.split('==')
                    current_block[pkey.strip()] = pval.strip()
                else:
                    raise AttributeError("""Got a line in the configuration file that isn't a block header nor a 
                    key=value.\nLine: {}""".format(strip_line))
            # add the last block of the file (if it's non-empty)
            if block_destination is not None and len(current_block) > 0:
                block_destination.append(current_block)

        # check number of configuration blocks for these configs
        if len(image_snap_config) > 1:
            raise AttributeError("More than one configuration block found for __IMAGE-SNAPSHOT__.")
        elif len(image_snap_config) == 1:     # had one config block
            image_snap_config = image_snap_config[0]
        if len(video_snap_config) > 1:
            raise AttributeError("More than one configuration block found for __VIDEO-SNAPSHOT__.")
        elif len(video_snap_config) == 1:     # had one config block
            video_snap_config = video_snap_config[0]
        if len(recording_config) > 1:
            raise AttributeError("More than one configuration block found for __PERSISTENT-RECORDING__.")
        elif len(recording_config) == 1:     # had one config block
            recording_config = recording_config[0]
        # log configs then return them
        logbook.notice("Camera configuration:", camera_config)
        logbook.notice("Image snapshot configuration:", image_snap_config)
        logbook.notice("Video snapshot configuration:", video_snap_config)
        logbook.notice("Persistent recording configuration:", recording_config)
        return camera_config, image_snap_config, video_snap_config, recording_config

    def _copy_config_file(self, config_file):
        """
        Copies the given configuration file to a new file inside the session directory, for future reference.
            The new file is called "this_session.config" and located inside self.session_absolute_directory.
        :param config_file: the original configuration file as defined by the user
        :return: absolute file path of config file copy
        """
        copy_filename = os.path.join(self.session_absolute_directory, "_SESSION_CONFIG.config")
        with open(config_file, 'r') as config_orig:
            with open(copy_filename, 'w') as config_copy:
                for line in config_orig:
                    config_copy.write(line)
        return copy_filename

    def _write_session_header_file(self):
        header_filename = os.path.join(self.session_absolute_directory, '_SESSION_INFO.txt')
        with open(header_filename, 'w') as f:
            f.write("SESSION #{}".format(self.this_session_number))
            f.write("\nINFORMATIONAL/HEADER FILE\n")
            f.write("-" * 50)
            # directory information
            f.write("\nDirectory (absolute): {}".format(self.session_absolute_directory))
            # time information
            timenow = datetime.datetime.now()
            unix_timenow = (timenow - datetime.datetime(year=1970, month=1, day=1)).total_seconds()
            f.write("\nSession initialization time (local): {} (UNIX: {})".format(timenow, unix_timenow))
            utctimenow = datetime.datetime.utcnow()
            unix_utctimenow = (utctimenow - datetime.datetime(year=1970, month=1, day=1)).total_seconds()
            f.write("\nSession initialization time (UTC): {} (UNIX: {})".format(utctimenow, unix_utctimenow))
            # camera information
            # TODO: put in config information for cameras, recording, etc
        return header_filename

    def initialize_gstd(self):
        """

        :return: None
        """
        # TODO: pass in connection parameters
        self.manager = GstdManager(gst_log=os.path.join(self.session_log_directory, 'gst.log'),
                                   gstd_log=os.path.join(self.session_log_directory, 'gstd.log'),
                                   gst_debug_level=5, tcp_enable=True, http_enable=False)
        self.manager.start()

    def initialize_gstd_client(self, num_retry=3):
        """
        Establish connection to GStreamer Daemon running on the system. Set up to retry connection due to some random
            connection issues that seem to resolve on retry.
        :param num_retry: Number of times to retry Gstd client connection before giving up.
        :return: None
        """
        # TODO: pass in connection parameters or connection mode
        gstd_py_logger = CustomLogger(logname='ingest_log', loglevel='DEBUG',
                                      logfile=os.path.join(self.session_log_directory, 'pygstc.log'))
        for i in range(num_retry):
            try:
                self.client = GstdClient(ip='localhost', port=5000, logger=gstd_py_logger)
                break
            except GstcError:
                time.sleep(1)
                if i == num_retry - 1:
                    print_exc()
                else:
                    logbook.warn("Connection failure #{}. Retry connecting to Gstd.".format(i + 1))
        else:
            logbook.critical("Problem with Gstreamer Daemon.")
            raise RuntimeError("Could not contact Gstd after {} attempts.".format(num_retry))
        self.client.debug_enable(True)

    def _construct_camera_pipelines(self):
        """
        # ----------------------------------------------------------------------------------------------------------
        # Each camera pipeline is independent, and constructed as follows.
        #
        #  rtspsrc --> rtph264depay --> h264parse --> queue --> interpipesink
        #
        # ----------------------------------------------------------------------------------------------------------
        """
        for single_camera_config in self.camera_config:
            cam_name = single_camera_config['name']
            if cam_name in self.pipelines_cameras:
                logbook.critical("Problem with camera configuration.")
                raise AttributeError("Camera name collision. Check configuration file.")
            # determine connection method and assemble URI
            if 'rtsp_authentication' in single_camera_config and 'rtsp_address' in single_camera_config:
                cam_connect = 'rtsp://{}@{}'.format(single_camera_config['rtsp_authentication'],
                                                    single_camera_config['rtsp_address'])
                cam_source = 'rtspsrc location={}'.format(cam_connect)
            else:
                # only RTSP implemented right now
                logbook.critical("Problem with camera configuration.")
                raise AttributeError("Need rtsp_authentication and rtsp_address in each camera config block.")
            logbook.info("Source for camera={}: {}".format(cam_name, cam_source))
            cam_sink = 'interpipesink name={} forward-events=true forward-eos=true sync=false'.format(
                PIPE_SINK_NAME_FORMATTER.format(cam_name))
            pd = '{} ! rtph264depay ! h264parse ! queue ! {}'.format(cam_source, cam_sink)
            cam = PipelineEntity(self.client, cam_name, pd)
            self.pipelines_cameras[cam_name] = cam

    def _construct_persistent_recording_pipeline(self):
        """
        # ----------------------------------------------------------------------------------------------------------
        # Persistent recording pipeline is a single pipeline definition, but contains multiple sub-pipelines
        #   - this way all the sub-pipelines start at the same time with one command
        # ----------------------------------------------------------------------------------------------------------
        #
        # interpipesrc (cam0) --> splitmuxsink
        #
        # interpipesrc (cam1) --> splitmuxsink
        #
        #    |     |     |
        #    V     V     V
        #
        #
        # ----------------------------------------------------------------------------------------------------------
        """
        # construct the recording pipeline for all the cameras by adding them all to the same description
        pd = ''
        for cam_name in self.pipelines_cameras.keys():
            # listen to camera sink; no need to name this interpipesrc because it won't be changed
            pd += ' interpipesrc format=time allow-renegotiation=false listen-to={} ! '.format(
                PIPE_SINK_NAME_FORMATTER.format(cam_name))
            # name camera-specific filesink with pipeline name and camera name
            pd += 'splitmuxsink name={} async-finalize=true muxer-pad-map=x-pad-map,video=video_0'.format(
                PIPE_CAMERA_FILESINK_NAME_FORMATTER.format(self.persistent_record_name, cam_name))
        record_h264 = PipelineEntity(self.client, self.persistent_record_name, pd)
        # check the validity of the recording directory and filename
        file_location = self.recording_config.get('recording_filename', DEFAULT_RECORDING_FILENAME)
        # split path location into directory and filename
        file_dir, file_name = os.path.split(file_location)
        file_dir = os.path.join(self.session_absolute_directory, file_dir)
        # check that the file number formatter is present
        if '%d' not in file_name and not any(['%0{}d'.format(i) in file_name for i in range(10)]):
            logbook.critical("Problem with recording configuration.")
            raise AttributeError("Need to include '%d' or '%0Nd' (N:0-9) in  recording filename template.")
        # check if we need to create camera-specific directories, or just one directory
        if '{}' in file_dir:
            create_dirs = [file_dir.format(cam_name) for cam_name in self.pipelines_cameras.keys()]
        elif '{}' in file_name:
            create_dirs = [file_dir]
        else:
            # didn't find a camera name placeholder in either the file_dir or the file_name
            logbook.critical("Problem with recording configuration.")
            raise AttributeError("Need to camera name placeholder ('{}') in recording filename template.")
        # create the necessary directories if they don't exist
        try:
            for create_dir in create_dirs:
                if not os.path.exists(create_dir):
                    logbook.notice("Making directory for persistent recording: {}".format(create_dir))
                    os.mkdir(create_dir)
        except OSError as e:
            # FATAL EXCEPTION
            logbook.critical("Problem creating persistent recording directories. This is a fatal exception.")
            print_exc()
            raise e
        # maximum segment time for multi-segment recording; number of minutes * 60 s/min * 1e9 ns/s
        max_file_time_mins = float(self.recording_config.get('segment_time', DEFAULT_RECORDING_SEGMENT_DURATION))
        max_file_time_ns = int(max_file_time_mins * 60 * 1e9)
        # maximum number of files, per camera, that are kept in storage
        max_num_files = int(self.recording_config.get('maximum_segment_files', DEFAULT_NUMBER_STORED_SEGMENTS))
        # if maximum storage space specified, convert and replace max_num_files (automatic override)
        if 'maximum_camera_storage' in self.recording_config:
            max_storage_mb = float(self.recording_config['maximum_camera_storage']) * 1024
            # convert storage space to recording time
            max_storage_mins = max_storage_mb / ESTIMATED_CAMERA_BITRATE / 60
            # convert to number of files and truncate decimal
            max_num_files = int(max_storage_mins / max_file_time_mins)
            print("Maximum number of files set from maximum storage config value: {}".format(max_num_files))
        else:
            print("Maximum number of files set directly from config value: {}".format(max_num_files))
        # set filesink (splitmuxsink element) properties for location and file management
        for cam_name in self.pipelines_cameras.keys():
            cam_dir = (file_dir if '{}' not in file_dir else file_dir.format(cam_name))
            cam_file = (file_name if '{}' not in file_name else file_name.format(cam_name))
            cam_full_location = os.path.join(cam_dir, cam_file)
            print("Setting file path for camera {} to {}".format(cam_name, cam_full_location))
            record_h264.set_property(PIPE_CAMERA_FILESINK_NAME_FORMATTER.format(self.persistent_record_name, cam_name),
                                     'location', cam_full_location)
            record_h264.set_property(PIPE_CAMERA_FILESINK_NAME_FORMATTER.format(self.persistent_record_name, cam_name),
                                     'max-size-time', str(max_file_time_ns))
            record_h264.set_property(PIPE_CAMERA_FILESINK_NAME_FORMATTER.format(self.persistent_record_name, cam_name),
                                     'max-files', str(max_num_files))
        self.pipelines_video_rec[self.persistent_record_name] = record_h264

    def _construct_buffered_video_snapshot_pipeline(self):
        """
        # ----------------------------------------------------------------------------------------------------------
        # Video buffers are independent pipelines, each constructed as follows.
        #
        #  interpipesrc (camaera) --> queue (FIFO config) --> interpipesink
        #
        # ----------------------------------------------------------------------------------------------------------
        # Video snapshot pipeline records from all buffers, and is constructed as follows.
        #
        #                                    mp4mux
        #                                   __________
        #                                  /          \
        #  interpipesrc (buffer-cam0) --> | video_0    |
        #                                 |            |
        #  interpipesrc (buffer-cam1) --> | video_1    |
        #        |   |   |   |            |            |  --> filesink
        #        V   V   V   V            |            |
        #  interpipesrc (buffer-camN) --> | video_N    |
        #                                  \          /
        #                                   ----------
        # ----------------------------------------------------------------------------------------------------------
        """
        # Length of historical video buffer; number of seconds * 1e9 ns/s
        min_buffer_time = float(self.video_snap_config.get('buffer_time', DEFAULT_BUFFER_TIME)) * 1e9
        # Set max time at 105% min time
        overflow_time = min_buffer_time * 1.05
        # Set buffer memory overflow for safety; assume 2x camera bitrate (in parameters.py) * 1024^2 B/MB
        overflow_size = overflow_time * 2 * ESTIMATED_CAMERA_BITRATE * 1024 * 1024

        buffer_name_format = 'buffer_h264_{}'       # not going to need to access these later
        for cam_name, _ in self.pipelines_cameras.items():
            buffer_name = buffer_name_format.format(cam_name)
            buffer_source = 'interpipesrc name={} format=time listen-to={}'.format(
                PIPE_SOURCE_NAME_FORMATTER.format(buffer_name), PIPE_SINK_NAME_FORMATTER.format(cam_name))
            # Partial queue definition; other parameters set after pipeline creation.
            qname = 'fifo_queue_{}'.format(cam_name)
            # Set leaky=2 for FIFO; silent=true for no events; disable number of buffers limit.
            queue_def = 'queue name={} max-size-buffers=0 leaky=2 silent=true flush-on-eos=false'.format(qname)
            buffer_sink = 'interpipesink name={} forward-events=true forward-eos=true sync=false'.format(
                PIPE_SINK_NAME_FORMATTER.format(buffer_name))
            buffer_def = '{} ! {} ! {}'.format(buffer_source, queue_def, buffer_sink)
            new_buffer = PipelineEntity(self.client, buffer_name, buffer_def)
            # set buffer properties; first convert values to integers then strings
            new_buffer.set_property(qname, 'min-threshold-time', str(int(min_buffer_time)))
            new_buffer.set_property(qname, 'max-size-time', str(int(overflow_time)))
            new_buffer.set_property(qname, 'max-size-bytes', str(int(overflow_size)))
            self.pipelines_video_buffer[buffer_name] = new_buffer

        # Video snapshot - connects to queue-buffers from each camera, muxes, and file-sinks
        # ----------------------------------------------------------------------------------------------------------
        logbook.notice("CREATING VIDEO SNAPSHOT PIPELINE")
        pd = ''
        for ci, cam_name in enumerate(self.pipelines_cameras.keys()):
            this_buffer_name = buffer_name_format.format(cam_name)
            pd += ' interpipesrc format=time allow-renegotiation=false listen-to={} ! '.format(
                PIPE_SINK_NAME_FORMATTER.format(this_buffer_name))
            pd += 'snapmux.video_{}'.format(ci)
        # file location will be set later when the snapshot is triggered
        pd += ' mp4mux name=snapmux ! filesink name={}'.format(
            PIPE_SINGLE_FILESINK_NAME_FORMATTER.format(self.video_snap_name))
        snap_video = PipelineEntity(self.client, self.video_snap_name, pd)
        self.pipelines_snap[self.video_snap_name] = snap_video

    def _construct_image_snapshot_pipeline(self):
        """
        # ----------------------------------------------------------------------------------------------------------
        # H.264 to still image transcoder is camera-generic and constructed as follows.
        #
        #  interpipesrc --> avdec_h264 --> jpegenc --> interpipesink
        #
        # ----------------------------------------------------------------------------------------------------------
        # Image snapshot pipeline listens to still image encoder and is constructed as follows.
        #
        #  interpipesrc --> filesink
        #
        # ----------------------------------------------------------------------------------------------------------
        """
        # source 'listen-to' parameter set at an arbitrary camera for now; changed during snapshot
        encoder_source = 'interpipesrc name={} format=time listen-to={}_sink'.format(
            PIPE_SOURCE_NAME_FORMATTER.format(self.image_encoder_name), list(self.pipelines_cameras.keys())[0])
        # not using jpegenc snapshot parameter (sends EOS after encoding a frame) because of H.264 key frames
        encoder_type = 'jpegenc quality=95'
        encoder_sink = 'interpipesink name={} '.format(PIPE_SINK_NAME_FORMATTER.format(self.image_encoder_name))
        encoder_sink += 'forward-events=true forward-eos=true sync=false async=false enable-last-sample=false drop=true'
        encoder_def = '{} ! avdec_h264 ! {} ! {}'.format(encoder_source, encoder_type, encoder_sink)
        image_encoder = PipelineEntity(self.client, self.image_encoder_name, encoder_def)
        self.pipelines_video_enc[self.image_encoder_name] = image_encoder

        # image snapshot - connects to only one camera at a time via image_encode pipeline and dumps a frame to file
        # ----------------------------------------------------------------------------------------------------------
        logbook.notice("CREATING IMAGE SNAPSHOT PIPELINE")
        snap_source = 'interpipesrc name={} format=time listen-to={} num-buffers=1'.format(
            PIPE_SOURCE_NAME_FORMATTER.format(self.image_snap_name),
            PIPE_SINK_NAME_FORMATTER.format(self.image_encoder_name))
        # file location will be set later when the snapshot is triggered
        snap_sink = 'filesink name={}'.format(PIPE_SINGLE_FILESINK_NAME_FORMATTER.format(self.image_snap_name))
        snap_image = PipelineEntity(self.client, self.image_snap_name, '{} ! {}'.format(snap_source, snap_sink))
        self.pipelines_snap[self.image_snap_name] = snap_image

    def construct_pipelines(self):
        """
        Construct all pipelines based on the configuration variables for this session.
        :return: None
        """
        try:
            # Create camera pipelines
            # ----------------------------------------------------------------------------------------------------------
            logbook.notice("\nCREATING CAMERA PIPELINES")
            self._construct_camera_pipelines()

            # H.264 recording via MPEG4 container mux to parallel streams
            # ----------------------------------------------------------------------------------------------------------
            if len(self.recording_config) > 0 and self.recording_config.get('enable', 'false').lower() == 'true':
                logbook.notice("\nCREATING PERSISTENT RECORDING PIPELINES")
                self._construct_persistent_recording_pipeline()

            # Camera FIFO historical video buffers for video snapshot capability
            # ----------------------------------------------------------------------------------------------------------
            if len(self.video_snap_config) > 0 and self.video_snap_config.get('enable', 'false').lower() == 'true':
                logbook.notice("\nCREATING BUFFER PIPELINES")
                self._construct_buffered_video_snapshot_pipeline()

            # H.264 to still image transcoder for image snapshot capability
            # ----------------------------------------------------------------------------------------------------------
            if len(self.image_snap_config) > 0 and self.image_snap_config.get('enable', 'false').lower() == 'true':
                logbook.notice("\nCREATING STILL IMAGE ENCODER PIPELINE")
                self._construct_image_snapshot_pipeline()

        except (GstcError, GstdError) as e:
            logbook.critical("Failure during pipeline construction.")
            print_exc()
            self.deconstruct_all_pipelines()
            self.kill_gstd()
            raise e

    def start_cameras(self):
        """
        Start each camera stream pipeline. Raises error if unsuccessful.
        :return: None
        """
        try:
            logbook.notice("Starting camera streams.")
            for pipeline_name, pipeline in self.pipelines_cameras.items():
                print("Starting {}.".format(pipeline_name))
                pipeline.play()
            time.sleep(5)
            logbook.notice("Camera streams initialized.")
        except (GstcError, GstdError) as e:
            logbook.critical("Could not initialize camera streams.")
            print_exc()
            self.deconstruct_all_pipelines()
            self.kill_gstd()
            raise e

    def start_buffers(self):
        """
        Start the video buffer pipelines so they start holding backlog.
        :return: None
        """
        try:
            logbook.notice("Starting camera stream buffers for video snapshot.")
            for pipeline_name, pipeline in self.pipelines_video_buffer.items():
                print("Starting {}.".format(pipeline_name))
                pipeline.play()
            time.sleep(1)
            logbook.notice("Camera stream buffers initialized.")
            logbook.notice("Buffers will reach capacity in {} seconds.".format(
                self.video_snap_config.get('buffer_time', DEFAULT_BUFFER_TIME)))
        except (GstcError, GstdError) as e:
            logbook.error("Could not initialize camera stream buffers.")
            print_exc()

    def start_persistent_recording_all_cameras(self):
        """
        Sets the persistent recording filename from the configuration file and starts the recording.
        :return: recording locations (with numbering formatter %0Nd) if successful; otherwise None
        """
        # get the location for the recordings
        # already checked for camera formatter and file number formatter in config parser
        # also already made the necessary directories
        # not allowed to change this persistent recording location for consistency across start/stops
        file_location = self.recording_config.get('recording_filename', DEFAULT_RECORDING_FILENAME)
        unformat_dir, unformat_file = os.path.split(file_location)
        fns = []
        try:
            # for each camera: format the filename, then set location param inside the pipeline splitmuxsink
            for cam_name in self.pipelines_cameras.keys():
                format_dir = (unformat_dir.format(cam_name) if '{}' in unformat_dir else unformat_dir)
                format_file = (unformat_file.format(cam_name) if '{}' in unformat_file else unformat_file)
                # by this point the file portion of the path has already been checked to contain '%0Nd'
                format_full_path = os.path.join(format_dir, format_file)
                # >> RIGHT NOW THESE ARE BEING SET IN THE PIPELINE CONSTRUCTION
                # print("Setting file location for {} to {}.".format(cam_name, format_full_path))
                # self.pipelines_video_rec[self.persistent_record_name].set_property(
                #     'multisink_{}'.format(cam_name), 'location', format_full_path)
                fns.append(format_full_path)
        except (GstcError, GstdError) as e:
            logbook.error("Could not set camera recording locations. Failed on {}".format(cam_name))
            print_exc()
            return None
        logbook.notice("Set file locations for all {} cameras.".format(len(self.pipelines_cameras)))
        # start the whole recording pipeline
        logbook.notice("Starting recording.")
        try:
            self.pipelines_video_rec[self.persistent_record_name].play()
            time.sleep(5)
            logbook.notice("Persistent recording pipeline playing.")
        except (GstcError, GstdError):
            logbook.error("Couldn't play persistent recording pipeline.")
            print_exc()
            return None
        return fns

    def stop_persistent_recording_all_cameras(self):
        """
        Send EOS to recording pipeline, then stop it.
        :return: None
        """
        try:
            logbook.notice("Sending EOS to persistent recording pipeline.")
            self.pipelines_video_rec[self.persistent_record_name].eos()
            logbook.notice("Waiting for recording to wrap up after EOS.")
            time.sleep(5)
            logbook.notice("Stopping persistent recording pipeline.")
            self.pipelines_video_rec[self.persistent_record_name].stop()
        except (GstcError, GstdError) as e:
            logbook.error("Problem with stopping persistent recording.")
            print_exc()

    def _image_snapshot_worker(self, camera_list, snap_abs_dir, snap_fn):
        """
        Executes the image snapshot given the final camera list and file location information.
            Meant to run in non-blocking mp.Process.
        :param camera_list: list of camera names to snapshot (list of strings assembled in calling function)
        :param snap_abs_dir: absolute directory for snapshot storage (optional '{}' formatter for camera name)
        :param snap_fn: snapshot file name (optional '{}' for camera name, requirement checked in calling function)
        :return: list of successful image snapshot filenames, if any (list can be empty)
        """
        # get the image snap and image encode pipelines
        snapimg_pipeline = self.pipelines_snap[self.image_snap_name]
        encode_img_pipeline = self.pipelines_video_enc[self.image_encoder_name]
        fns = []
        # run each camera independently
        # first check the directory existence and create if necessary
        for camera_name in camera_list:
            try:
                # add in camera name to directory if needed
                snap_abs_fmt_dir = (snap_abs_dir if '{}' not in snap_abs_dir else snap_abs_dir.format(camera_name))
                # make the directory if it doesn't exist
                if not os.path.exists(snap_abs_fmt_dir):
                    logbook.notice("Making directory: {}".format(snap_abs_fmt_dir))
                    os.mkdir(snap_abs_fmt_dir)
                # join the formatted absolute directory and the formatted (if applicable) filename
                snap_abs_fmt_fn = os.path.join(snap_abs_fmt_dir,
                                               (snap_fn if '{}' not in snap_fn else snap_fn.format(camera_name)))
                # set the location of the filesink in the image snap pipeline
                logbook.info("Setting location of {} pipeline filesink to {}.".format(self.image_snap_name, snap_abs_fmt_fn))
                snapimg_pipeline.set_property(PIPE_SINGLE_FILESINK_NAME_FORMATTER.format(self.image_snap_name),
                                              'location', snap_abs_fmt_fn)
            except (OSError, GstcError, GstdError):
                logbook.error("Problem setting up directory and setting filesink location.")
                print_exc()
                continue
            try:
                # set the encoding pipeline to listen to the appropriate camera
                logbook.info("Setting encoding interpipe to listen to {}.".format(camera_name))
                encode_img_pipeline.listen_to(PIPE_SINK_NAME_FORMATTER.format(camera_name))
                # play the image encoder pipeline and let it spin up (needs a key frame for proper H.264 decoding)
                logbook.info("Playing image encoder.")
                encode_img_pipeline.play()
                time.sleep(IMAGE_ENCODE_SPIN_UP)
                # run the snap image pipeline for a bit, not sure if this time matters much
                snapimg_pipeline.play()
                time.sleep(IMAGE_SNAP_EXECUTE_TIME)
                # TODO: EOS encoder?
                snapimg_pipeline.stop()
                encode_img_pipeline.stop()
                fns.append(snap_abs_fmt_fn)
            except (GstcError, GstdError):
                logbook.error("Problem with encoding/snapshot pipeline.")
                print_exc()
                continue
        logbook.notice("Image snapshot worker process complete.")
        logbook.notice("Snapshots: {}".format(fns))
        return fns

    def take_image_snapshot(self, file_relative_location, file_absolute_location=None, cameras='all'):
        """
        Takes a still image snapshot of each camera specified. They are taken one at a time in order to avoid spinning
            up numerous H.264->still transcoding pipelines. Failure of one snapshot will not prevent the others.
        :param file_relative_location: location inside session directory to store snapshots; if more than one camera
            is specified, then '{}' placeholder must be included in directory or filename portion for camera name
        :param file_absolute_location: same as relative location, but setting this != None automatically overrides it
        :param cameras: cameras to snapshot; 'all'=all cameras; list/tuple of camera names; ','-sep. str of camera names
        :return: None
        """
        # check if video snapshot pipeline was constructed
        if self.image_snap_name not in self.pipelines_snap or self.image_encoder_name not in self.pipelines_video_enc:
            logbook.error("Image snapshot pipeline or encoder pipeline wasn't constructed. Ignoring command.")
            return None
        # extract the camera list from the given parameter
        if cameras == 'all':
            camlist = list(self.pipelines_cameras.keys())
        elif type(cameras) in (list, tuple):
            camlist = cameras
        elif type(cameras) is str:
            # if it's just a single camera we'll still get a list
            camlist = cameras.split(',')
        else:
            logbook.error("""Got an invalid type for argument `cameras`. 
            Need list/tuple of str or comma-delineated str or 'all'.""")
            return None
        # check that all of the camera names are correct
        if not all([cam in self.pipelines_cameras for cam in camlist]):
            logbook.error("""One or more of the cameras specified for snapshot is not valid. 
            Available: {}. Specified: {}. Ignoring command.""".format(self.pipelines_cameras.keys(), cameras))
            return None
        logbook.notice("Snapping cameras: {}".format(camlist))
        # set the directory and filename from the absolute or relative parameters given
        if file_absolute_location is None:
            snap_dir, snap_fn = os.path.split(file_relative_location)
            snap_abs_dir = os.path.join(self.session_absolute_directory, snap_dir)
        else:
            snap_abs_dir, snap_fn = os.path.split(file_absolute_location)
        # check if there are multiple cameras and make sure the placeholder is included
        if len(camlist) > 1 and ('{}' not in snap_fn and '{}' not in snap_abs_dir):
            logbook.error("More than one camera given for snapshot, but no '{}' in file location. Ignoring command.")
            return None

        imgsnap = multiprocessing.Process(target=self._image_snapshot_worker,
                                          args=(camlist, snap_abs_dir, snap_fn))
        logbook.notice("Starting image snapshot worker process.")
        try:
            imgsnap.start()
            logbook.notice("Process started, exiting blocking function.")
        except multiprocessing.ProcessError:
            logbook.error("Problem starting image snap worker process.")
            print_exc()
        return None

    def _video_snapshot_worker(self, duration, snapshot_file_absolute_location):
        """
        Executes the video snapshot given the final duration and file location. Meant to run in non-blocking mp.Process.
        :param duration: duration of video snapshot in seconds
        :param snapshot_file_absolute_location: absolute file path for video snapshot
        :return: snapshot_file_absolute_location if successful
        """
        try:
            snapvid_pipeline = self.pipelines_snap[self.video_snap_name]
            logbook.info("Setting filesink location of video snapshot pipeline.")
            snapvid_pipeline.set_property(PIPE_SINGLE_FILESINK_NAME_FORMATTER.format(self.video_snap_name),
                                          'location', snapshot_file_absolute_location)
            logbook.info("Playing {} pipeline.".format(self.video_snap_name))
            snapvid_pipeline.play()
            logbook.info("Waiting for {} seconds of recording time...".format(duration))
            time.sleep(duration)
            logbook.info("Sending EOS and stop to {} pipeline.".format(self.video_snap_name))
            snapvid_pipeline.eos()
            snapvid_pipeline.stop()
            logbook.info("Video snapshot complete to {}.".format(snapshot_file_absolute_location))
            return snapshot_file_absolute_location
        except (GstdError, GstcError):
            logbook.error("Problem with video snapshot.")
            print_exc()
            return None

    def take_video_snapshot(self, duration, file_relative_location, file_absolute_location=None):
        """
        Takes a snapshot of video from each camera, beginning with the buffered backlog of video. This allows the
            video snapshot trigger to grab video from a little while ago. Buffer length specified in config file.
        :param duration: duration of video snapshot in seconds (min=5; max=3600)
        :param file_relative_location: relative location (directory + filename) inside session storage directory
        :param file_absolute_location: (overrides relative location) absolute directory + filename
        :return: None
        """
        # check if video snapshot pipeline was constructed
        if self.video_snap_name not in self.pipelines_snap:
            logbook.error("Video snapshot pipeline wasn't constructed. Ignoring command.")
            return None
        # check duration limits
        if duration > 3600:
            logbook.error("Video snapshot duration is too high. Limit = 1 hour (3600 seconds). Ignoring command.")
            return None
        elif duration < 5:
            logbook.error("Video snapshot duration is too short. Minimum = 5 seconds. Ignoring command.")
            return None
        # decide directory from relative vs. absolute
        if file_absolute_location is None:
            snap_dir, snap_fn = os.path.split(file_relative_location)
            if snap_dir.startswith('./'):
                snap_dir = snap_dir[2:]
                logbook.info("Reformatting relative location to {}".format(snap_dir))
            elif snap_dir.startswith('/'):
                snap_dir = snap_dir[1:]
                logbook.info("Reformatting relative location to {}".format(snap_dir))
            snap_abs_dir = os.path.join(self.session_absolute_directory, snap_dir)
            logbook.info("Relative file location inside session directory: {}".format(snap_abs_dir))
        else:
            snap_abs_dir, snap_fn = os.path.split(file_absolute_location)
            logbook.info("Absolute file location directory override for video snap: {}".format(snap_abs_dir))
        # make the directory, if needed, then set the file location
        try:
            if not os.path.exists(snap_abs_dir):
                logbook.notice("Video snapshot directory doesn't exist. Making it now.")
                os.mkdir(snap_abs_dir)
        except OSError:
            logbook.error("Problem making directory.")
            print_exc()
            return None
        snap_abs_fn = os.path.join(snap_abs_dir, snap_fn)

        vidsnap = multiprocessing.Process(target=self._video_snapshot_worker,
                                          args=(duration, snap_abs_fn))
        logbook.notice("Starting video snapshot worker process.")
        try:
            vidsnap.start()
            logbook.notice("Process started, exiting blocking function.")
        except multiprocessing.ProcessError:
            logbook.error("Problem starting video snap worker process.")
            print_exc()
        return None

    def stop_all_pipelines(self):
        """
        Sends EOS signal to recording and encoder pipelines, then calls stop command on all instantiated pipelines.
        :return: None
        """
        logbook.notice("Sending EOS for relevant pipelines and stopping all pipelines. This will take a few seconds.")
        # send the end of stream (EOS) signals first
        for group in (self.pipelines_video_rec, self.pipelines_video_enc):
            for pipeline_name, pipeline in group.items():
                try:
                    logbook.info("Sending EOS to {}.".format(pipeline_name))
                    pipeline.eos()
                except:
                    logbook.warning("Couldn't EOS {}.".format(pipeline_name))
                    print_exc()
        logbook.info("Waiting a bit for EOS to take effect.")
        time.sleep(10)
        # now stop each pipeline
        for group in (self.pipelines_snap, self.pipelines_video_rec, self.pipelines_video_enc,
                      self.pipelines_video_buffer, self.pipelines_cameras):
            for pipeline_name, pipeline in group.items():
                try:
                    logbook.info("Stopping {}.".format(pipeline_name))
                    pipeline.stop()
                except:
                    logbook.warning("Couldn't stop {}.".format(pipeline_name))

    def deconstruct_all_pipelines(self):
        """
        Calls the pipeline delete command on all instantiated pipelines.
        :return: None
        """
        logbook.notice("Deconstructing all pipelines.")
        for group in (self.pipelines_snap, self.pipelines_video_rec, self.pipelines_video_enc,
                      self.pipelines_video_buffer, self.pipelines_cameras):
            for pipeline_name, pipeline in group.items():
                try:
                    logbook.info("Deleting {} pipeline.".format(pipeline_name))
                    pipeline.delete()
                    logbook.info("Deleted {}.".format(pipeline_name))
                except (GstcError, GstdError):
                    logbook.warning("Exception while deleting {}.".format(pipeline_name))
                    print_exc()

    def kill_gstd(self):
        """
        Stops the GstdManager that was instantiated for this IngestSession.
        return: None
        """
        logbook.notice("Stopping Gstreamer Daemon.")
        self.manager.stop()


def main():
    session = IngestSession(session_root_directory='/home/dev/Videos/ingest_pipeline',
                            session_config_file='./sample.config')
    try:
        session.construct_pipelines()
        session.start_cameras()
        session.start_buffers()
        session.start_persistent_recording_all_cameras()
        time.sleep(30)
        session.take_image_snapshot(cameras='all', file_relative_location='imgsnap/snap_{}.jpg')
        session.take_video_snapshot(duration=35, file_relative_location='/vidsnap/snap0.mp4')
        print("Wait 65")
        time.sleep(65)
        print("Done waiting")
        session.stop_persistent_recording_all_cameras()
    except KeyboardInterrupt:
        print_exc()
    finally:
        session.stop_all_pipelines()
        session.deconstruct_all_pipelines()
        session.kill_gstd()

if __name__ == '__main__':
    main()
