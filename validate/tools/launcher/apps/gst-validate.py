#!/usr/bin/env python2
#
# Copyright (c) 2013,Thibault Saunier <thibault.saunier@collabora.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin St, Fifth Floor,
# Boston, MA 02110-1301, USA.
import os
import time
import urlparse
import subprocess
import ConfigParser
import xml.etree.ElementTree as ET
from loggable import Loggable

from baseclasses import GstValidateTest, TestsManager, Test, ScenarioManager, NamedDic
from utils import MediaFormatCombination, get_profile,\
    path2url, DEFAULT_TIMEOUT, which, GST_SECOND, Result, \
    compare_rendered_with_original, Protocols

######################################
#       Private global variables     #
######################################

# definitions of commands to use
GST_VALIDATE_COMMAND = "gst-validate-1.0"
GST_VALIDATE_TRANSCODING_COMMAND = "gst-validate-transcoding-1.0"
G_V_DISCOVERER_COMMAND = "gst-validate-media-check-1.0"
if "win32" in sys.platform:
    GST_VALIDATE_COMMAND += ".exe"
    GST_VALIDATE_TRANSCODING_COMMAND += ".exe"
    G_V_DISCOVERER_COMMAND += ".exe"

# Some extension file for discovering results
G_V_MEDIA_INFO_EXT = "media_info"
G_V_STREAM_INFO_EXT = "stream_info"


#################################################
#       API to be used to create testsuites     #
#################################################

"""
A list of tuple of the form:
 (@regex_defining_blacklister_test_names, @reson_for_the_blacklisting)
"""
GST_VALIDATE_BLACKLISTED_TESTS = []

"""
A list of scenario names to be run
"""
GST_VALIDATE_SCENARIOS = []

"""
A list of #GstValidateTestGenerator to be used to generate tests
"""
GST_VALIDATE_TEST_GENERATORS = []

"""
A list of #MediaFormatCombinations describing wanted output
formats for transcoding test
"""
GST_VALIDATE_ENCODING_FORMATS = []


"""
Some info about protocols and how to handle them
"""
GST_VALIDATE_CAPS_TO_PROTOCOL = [("application/x-hls", Protocols.HLS)]
GST_VALIDATE_PROTOCOL_TIMEOUTS = {Protocols.HTTP: 120,
                                  Protocols.HLS: 240}

class GstValidateMediaDescriptor(Loggable):
    def __init__(self, xml_path):
        Loggable.__init__(self)
        self._xml_path = xml_path
        self.media_xml = ET.parse(xml_path).getroot()

       # Sanity checks
        self.media_xml.attrib["duration"]
        self.media_xml.attrib["seekable"]

    def get_media_filepath(self):
        if self.get_protocol() == Protocols.FILE:
            return self._xml_path.replace("." + G_V_MEDIA_INFO_EXT, "")
        else:
            return self._xml_path.replace("." + G_V_STREAM_INFO_EXT, "")


    def get_caps(self):
        return self.media_xml.findall("streams")[0].attrib["caps"]

    def get_uri(self):
        return self.media_xml.attrib["uri"]

    def get_duration(self):
        return long(self.media_xml.attrib["duration"])

    def set_protocol(self, protocol):
        self.media_xml.attrib["protocol"] = protocol

    def get_protocol(self):
        return self.media_xml.attrib["protocol"]

    def is_seekable(self):
        return self.media_xml.attrib["seekable"]

    def is_image(self):
        for stream in self.media_xml.findall("streams")[0].findall("stream"):
            if stream.attrib["type"] == "image":
                return True
        return False

    def get_num_tracks(self, track_type):
        n = 0
        for stream in self.media_xml.findall("streams")[0].findall("stream"):
            if stream.attrib["type"] == track_type:
                n += 1

        return n

    def is_compatible(self, scenario):
        if scenario.seeks() and (not self.is_seekable() or self.is_image()):
            self.debug("Do not run %s as %s does not support seeking",
                       scenario, self.get_uri())
            return False

        for track_type in ['audio', 'subtitle']:
            if self.get_num_tracks(track_type) < scenario.get_min_tracks(track_type):
                self.debug("%s -- %s | At least %s %s track needed  < %s"
                           % (scenario, self.get_uri(), track_type,
                              scenario.get_min_tracks(track_type),
                              self.get_num_tracks(track_type)))
                return False

        return True

class GstValidateTestGenerator(Loggable):
    def __init__(self, name, tests=[]):
        Loggable.__init__(self)
        self.name = name
        self._tests = tests
        self.set_config(None, None)

    def set_config(self, options, reporter):
        self.options = options
        self.reporter = reporter

    def populate_tests(self, uri_minfo_special_scenarios, scenarios):
        pass

    def generate_tests(self, uri_minfo_special_scenarios, scenarios):

        self.populate_tests(uri_minfo_special_scenarios, scenarios)
        return self._tests

    def add_test(self, test):
        self._tests.append(test)


class GstValidateMediaCheckTestGenerator(GstValidateTestGenerator):
    def __init__(self):
        GstValidateTestGenerator.__init__(self, "media_check")

    def populate_tests(self, uri_minfo_special_scenarios, scenarios):
        for uri, mediainfo, special_scenarios in uri_minfo_special_scenarios:
            protocol = mediainfo.media_descriptor.get_protocol()
            try:
                timeout = GST_VALIDATE_PROTOCOL_TIMEOUTS[protocol]
            except KeyError:
                timeout = DEFAULT_TIMEOUT

            classname = "validate.%s.media_check.%s" % (protocol,
                                                        os.path.basename(uri).replace(".", "_"))
            self.add_test(GstValidateMediaCheckTest(classname,
                                                    self.options,
                                                    self.reporter,
                                                    mediainfo.media_descriptor,
                                                    uri,
                                                    mediainfo.path,
                                                    timeout=timeout))


class GstValidateTranscodingTestGenerator(GstValidateTestGenerator):
    def __init__(self):
        GstValidateTestGenerator.__init__(self, "transcode")

    def populate_tests(self, uri_minfo_special_scenarios, scenarios):
        for uri, mediainfo, special_scenarios in uri_minfo_special_scenarios:
            if mediainfo.media_descriptor.is_image():
                continue

            for comb in GST_VALIDATE_ENCODING_FORMATS:
                classname = "validate.%s.transcode.to_%s.%s" % (mediainfo.media_descriptor.get_protocol(),
                                                                str(comb).replace(' ', '_'),
                                                                os.path.basename(uri).replace(".", "_"))
                self.add_test(GstValidateTranscodingTest(classname,
                                                         self.options,
                                                         self.reporter,
                                                         comb,
                                                         uri,
                                                         mediainfo.media_descriptor))


class GstValidatePipelineTestGenerator(GstValidateTestGenerator):
    def __init__(self, name, pipeline_template=None, pipelines_descriptions=None,
                 valid_scenarios=[]):
        """
        @name: The name of the generator
        @pipeline_template: A template pipeline to be used to generate actual pipelines
        @pipelines_descriptions: A list of tuple of the form:
                                 (test_name, pipeline_description)
        @valid_scenarios: A list of scenario name that can be used with that generator
        """
        GstValidateTestGenerator.__init__(self, name)
        self._pipeline_template = pipeline_template
        self._pipelines_descriptions = pipelines_descriptions
        self._valid_scenarios = valid_scenarios

    def get_fname(self, scenario, protocol=None, name=None):
        if name is None:
            name = self.name

        if protocol is not None:
            protocol_str = "%s." % protocol
        else:
            protocol_str = ""

        if scenario is not None and scenario.name.lower() != "none":
            return "%s.%s%s.%s" % ("validate", protocol_str, name, scenario.name)

        return "%s.%s%s" % ("validate", protocol_str, name)

    def generate_tests(self, uri_minfo_special_scenarios, scenarios):

        if self._valid_scenarios:
            scenarios = [scenario for scenario in scenarios if
                          scenario.name in self._valid_scenarios]

        return super(GstValidatePipelineTestGenerator, self).generate_tests(
              uri_minfo_special_scenarios, scenarios)

    def populate_tests(self, uri_minfo_special_scenarios, scenarios):
        for name, pipeline in self._pipelines_descriptions:
            for scenario in scenarios:
                fname = self.get_fname(scenario, name=name)
                self.add_test(GstValidateLaunchTest(fname,
                                                    self.options,
                                                    self.reporter,
                                                    pipeline,
                                                    scenario=scenario)
                              )


class GstValidatePlaybinTestGenerator(GstValidatePipelineTestGenerator):

    def __init__(self):
        GstValidatePipelineTestGenerator.__init__(self, "playback", "playbin")

    def populate_tests(self, uri_minfo_special_scenarios, scenarios):
        for uri, minfo, special_scenarios in uri_minfo_special_scenarios:
            pipe = self._pipeline_template
            protocol = minfo.media_descriptor.get_protocol()

            if self.options.mute:
                fakesink = "'fakesink sync=true'"
                pipe += " audio-sink=%s video-sink=%s" %(fakesink, fakesink)

            pipe += " uri=%s" % uri
            for scenario in special_scenarios + scenarios:
                if not minfo.media_descriptor.is_compatible(scenario):
                    continue

                fname = "%s.%s" % (self.get_fname(scenario,
                                   protocol),
                                   os.path.basename(uri).replace(".", "_"))
                self.debug("Adding: %s", fname)

                if scenario.does_reverse_playback() and protocol == Protocols.HTTP:
                    # 10MB so we can reverse playback
                    pipe += " ring-buffer-max-size=10485760"

                self.add_test(GstValidateLaunchTest(fname,
                                                    self.options,
                                                    self.reporter,
                                                    pipe,
                                                    scenario=scenario,
                                                    media_descriptor=minfo.media_descriptor)
                              )


class GstValidateLaunchTest(GstValidateTest):
    def __init__(self, classname, options, reporter, pipeline_desc,
                 timeout=DEFAULT_TIMEOUT, scenario=None, media_descriptor=None):
        try:
            timeout = GST_VALIDATE_PROTOCOL_TIMEOUTS[media_descriptor.get_protocol()]
        except KeyError:
            pass
        except AttributeError:
            pass

        duration = 0
        if scenario:
            duration = scenario.get_duration()
        elif media_descriptor:
            duration = media_descriptor.get_duration() / GST_SECOND
        super(GstValidateLaunchTest, self).__init__(GST_VALIDATE_COMMAND, classname,
                                              options, reporter,
                                              duration=duration,
                                              scenario=scenario,
                                              timeout=timeout)

        self.pipeline_desc = pipeline_desc
        self.media_descriptor = media_descriptor

    def build_arguments(self):
        GstValidateTest.build_arguments(self)
        self.add_arguments(self.pipeline_desc)

    def get_current_value(self):
        if self.scenario:
            sent_eos = self.sent_eos_position()
            if sent_eos is not None:
                t = time.time()
                if ((t - sent_eos)) > 30:
                    if self.media_descriptor.get_protocol() == Protocols.HLS:
                        self.set_result(Result.PASSED,
                                        """Got no EOS 30 seconds after sending EOS,
                                        in HLS known and tolerated issue:
                                        https://bugzilla.gnome.org/show_bug.cgi?id=723868""")
                        return Result.KNOWN_ERROR

                    self.set_result(Result.FAILED, "Pipeline did not stop 30 Seconds after sending EOS")

                    return Result.FAILED

        return self.get_current_position()


class GstValidateMediaCheckTest(Test):
    def __init__(self, classname, options, reporter, media_descriptor, uri, minfo_path,
                 timeout=DEFAULT_TIMEOUT):
        super(GstValidateMediaCheckTest, self).__init__(G_V_DISCOVERER_COMMAND, classname,
                                              options, reporter,
                                              timeout=timeout)
        self._uri = uri
        self.media_descriptor = media_descriptor
        self._media_info_path = minfo_path

    def build_arguments(self):
        self.add_arguments(self._uri, "--expected-results",
                           self._media_info_path)


class GstValidateTranscodingTest(GstValidateTest):
    _scenarios = ScenarioManager()
    def __init__(self, classname, options, reporter,
                 combination, uri, media_descriptor,
                 timeout=DEFAULT_TIMEOUT,
                 scenario=None):

        Loggable.__init__(self)

        file_dur = long(media_descriptor.get_duration()) / GST_SECOND
        try:
            timeout = GST_VALIDATE_PROTOCOL_TIMEOUTS[media_descriptor.get_protocol()]
        except KeyError:
            pass

        super(GstValidateTranscodingTest, self).__init__(
            GST_VALIDATE_TRANSCODING_COMMAND, classname,
            options, reporter, duration=file_dur,
            timeout=timeout, scenario=scenario)

        self.media_descriptor = media_descriptor
        self.uri = uri
        self.combination = combination
        self.dest_file = ""

    def set_rendering_info(self):
        self.dest_file = path = os.path.join(self.options.dest,
                                             self.classname.replace(".transcode.", os.sep).
                                             replace(".", os.sep))
        utils.mkdir(os.path.dirname(urlparse.urlsplit(self.dest_file).path))
        if urlparse.urlparse(self.dest_file).scheme == "":
            self.dest_file = path2url(self.dest_file)

        profile = get_profile(self.combination)
        self.add_arguments("-o", profile)

    def build_arguments(self):
        GstValidateTest.build_arguments(self)
        self.set_rendering_info()
        self.add_arguments(self.uri, self.dest_file)

    def get_current_value(self):
        if self.scenario:
            sent_eos = self.sent_eos_position()
            if sent_eos is not None:
                t = time.time()
                if ((t - sent_eos)) > 30:
                    if self.media_descriptor.get_protocol() == Protocols.HLS:
                        self.set_result(Result.PASSED,
                                        """Got no EOS 30 seconds after sending EOS,
                                        in HLS known and tolerated issue:
                                        https://bugzilla.gnome.org/show_bug.cgi?id=723868""")
                        return Result.KNOWN_ERROR

                    self.set_result(Result.FAILED, "Pipeline did not stop 30 Seconds after sending EOS")

                    return Result.FAILED

        return self.get_current_size()

    def check_results(self):
        if self.result in [Result.FAILED, Result.TIMEOUT]:
            GstValidateTest.check_results(self)
            return

        if self.scenario:
            orig_duration = min(long(self.scenario.get_duration()),
                                long(self.media_descriptor.get_duration()))
        else:
            orig_duration = long(self.media_descriptor.get_duration())
        res, msg = compare_rendered_with_original(orig_duration, self.dest_file)
        self.set_result(res, msg)


class GstValidateManager(TestsManager, Loggable):

    name = "validate"
    _scenarios = ScenarioManager()


    def __init__(self):
        TestsManager.__init__(self)
        Loggable.__init__(self)
        self._uris = []
        self._run_defaults = True
        self._is_populated = False

    def init(self):
        if which(GST_VALIDATE_COMMAND) and which(GST_VALIDATE_TRANSCODING_COMMAND):
            return True
        return False

    def add_options(self, parser):
        group = parser.add_argument_group("GstValidate tools specific options"
                            " and behaviours",
description="""When using --wanted-tests, all the scenarios can be used, even those which have
not been tested and explicitely activated, in order to only use those, you should
use --wanted-tests defaults_only""")

        group.add_argument("-vc", "--validate-config", dest="validate_config",
                           default=None,
help="""Lets you specify a file where the testsuite to execute is defined.
In this file, your will be able to access the following variables:
   * GST_VALIDATE_SCENARIOS: A list of scenario names to be run
   * GST_VALIDATE_BLACKLISTED_TESTS: A list of tuple of the form:
         (@regex_defining_blacklister_test_names, @reason_for_the_blacklisting)
   * GST_VALIDATE_TEST_GENERATORS: A list of #GstValidateTestGenerator to be used to generate tests
   * GST_VALIDATE_ENCODING_FORMATS: A list of #MediaFormatCombination to be used for transcoding tests

You can also set default values with:
    * gst_validate_register_defaults: Sets default values for all parametters
    * gst_validate_register_default_test_generators: Sets default values for the TestGenerators to be used
    * gst_validate_register_default_scenarios: Sets default values for the scenarios to be executed
    * gst_validate_register_default_encoding_formats: Sets default values for the encoding formats to be tested
""")

    def _populate_testsuite(self, options):

        if self._is_populated is True:
            return

        if options.validate_config:
            execfile(options.validate_config, globals())
        else:
            gst_validate_register_defaults()

        self._is_populated = True

    def list_tests(self):
        if self.tests:
            return self.tests

        self._populate_testsuite(self.options)

        if self._run_defaults:
            scenarios = [self._scenarios.get_scenario(scenario_name)
                         for scenario_name in GST_VALIDATE_SCENARIOS]
        else:
            scenarios = self._scenarios.get_scenario(None)
        uris = self._list_uris()

        for generator in GST_VALIDATE_TEST_GENERATORS:
            generator.set_config(self.options, self.reporter)
            for test in generator.generate_tests(uris, scenarios):
                self.add_test(test)

        return self.tests

    def _check_discovering_info(self, media_info, uri=None):
        self.debug("Checking %s", media_info)
        media_descriptor = GstValidateMediaDescriptor(media_info)
        try:
            # Just testing that the vairous mandatory infos are present
            caps = media_descriptor.get_caps()
            if uri is None:
                uri = media_descriptor.get_uri()

            media_descriptor.set_protocol(urlparse.urlparse(uri).scheme)
            for caps2, prot in GST_VALIDATE_CAPS_TO_PROTOCOL:
                if caps2 == caps:
                    media_descriptor.set_protocol(prot)
                    break

            scenario_bname = media_descriptor.get_media_filepath()
            special_scenarios = self._scenarios.find_special_scenarios(scenario_bname)
            self._uris.append((uri,
                               NamedDic({"path": media_info,
                                         "media_descriptor": media_descriptor}),
                               special_scenarios))
        except ConfigParser.NoOptionError as e:
            self.debug("Exception: %s for %s", e, media_info)

    def _discover_file(self, uri, fpath):
        try:
            media_info = "%s.%s" % (fpath, G_V_MEDIA_INFO_EXT)
            args = G_V_DISCOVERER_COMMAND.split(" ")
            args.append(uri)
            if os.path.isfile(media_info):
                self._check_discovering_info(media_info, uri)
                return True
            elif fpath.endswith(G_V_STREAM_INFO_EXT):
                self._check_discovering_info(fpath)
                return True
            elif self.options.generate_info:
                args.extend(["--output-file", media_info])
            else:
                return True

            subprocess.check_output(args)
            self._check_discovering_info(media_info, uri)

            return True

        except subprocess.CalledProcessError as e:
            self.debug("Exception: %s", e)
            return False

    def _list_uris(self):
        if self._uris:
            return self._uris

        if not self.args:
            if isinstance(self.options.paths, str):
                self.options.paths = [os.path.join(self.options.paths)]

            for path in self.options.paths:
                for root, dirs, files in os.walk(path):
                    for f in files:
                        fpath = os.path.join(path, root, f)
                        if os.path.isdir(fpath) or \
                                fpath.endswith(G_V_MEDIA_INFO_EXT) or\
                                fpath.endswith(ScenarioManager.FILE_EXTENDION):
                            continue
                        else:
                            self._discover_file(path2url(fpath), fpath)

        self.debug("Uris found: %s", self._uris)

        return self._uris

    def needs_http_server(self):
        for test in self.list_tests():
            if self._is_test_wanted(test) and test.media_descriptor is not None:
                protocol = test.media_descriptor.get_protocol()
                uri = test.media_descriptor.get_uri()

                if protocol == Protocols.HTTP and \
                    "127.0.0.1:%s" % (self.options.http_server_port) in uri:
                    return True
        return False

    def get_blacklisted(self, options):
        self._populate_testsuite(options)

        return GST_VALIDATE_BLACKLISTED_TESTS

    def set_settings(self, options, args, reporter):
        TestsManager.set_settings(self, options, args, reporter)
        if options.wanted_tests and not [d for d in options.wanted_tests
                                         if "defaults_only" in d]:
            self._run_defaults = False


#################################################
# GstValidate default testsuite implementation     #
#################################################


def gst_validate_register_default_test_generators():
    """
    Registers default test generators
    """
    GST_VALIDATE_TEST_GENERATORS.append(GstValidatePlaybinTestGenerator())
    GST_VALIDATE_TEST_GENERATORS.append(GstValidateMediaCheckTestGenerator())
    GST_VALIDATE_TEST_GENERATORS.append(GstValidateTranscodingTestGenerator())


def gst_validate_register_default_scenarios():
    """
    Registers default test scenarios
    """
    GST_VALIDATE_SCENARIOS.extend([
                 "play_15s",
                 "reverse_playback",
                 "fast_forward",
                 "seek_forward",
                 "seek_backward",
                 "seek_with_stop",
                 "switch_audio_track",
                 "switch_audio_track_while_paused",
                 "switch_subtitle_track",
                 "switch_subtitle_track_while_paused",
                 "disable_subtitle_track_while_paused",
                 "change_state_intensive",
                 "scrub_forward_seeking"])

def gst_validate_register_default_encoding_formats():
    """
    Registers default encoding formats
    """
    GST_VALIDATE_ENCODING_FORMATS.extend([
        MediaFormatCombination("ogg", "vorbis", "theora"),
        MediaFormatCombination("webm", "vorbis", "vp8"),
        MediaFormatCombination("mp4", "mp3", "h264"),
        MediaFormatCombination("mkv", "vorbis", "h264"),
    ])

def gst_validate_define_default_blacklist():
    GST_VALIDATE_BLACKLISTED_TESTS.extend([
        ("validate.hls.playback.fast_forward.*",
         "https://bugzilla.gnome.org/show_bug.cgi?id=698155"),
        ("validate.hls.playback.seek_with_stop.*",
         "https://bugzilla.gnome.org/show_bug.cgi?id=723268"),
        ("validate.hls.playback.reverse_playback.*",
         "https://bugzilla.gnome.org/show_bug.cgi?id=702595"),
        ("validate.hls.*scrub_forward_seeking.*", "This is not stable enough for now."),

        # Matroska/WEBM known issues:
        ("validate.*.reverse_playback.*webm$",
         "https://bugzilla.gnome.org/show_bug.cgi?id=679250"),
        ("validate.*reverse.*Sintel_2010_720p_mkv",
         "TODO in matroskademux: FIXME: We should build an index during playback or "
         "when scanning that can be used here. The reverse playback code requires "
         " seek_index and seek_entry to be set!"),
        ("validate.http.playback.seek_with_stop.*webm",
         "matroskademux.gst_matroska_demux_handle_seek_push: Seek end-time not supported in streaming mode"),
        ("validate.http.playback.seek_with_stop.*mkv",
         "matroskademux.gst_matroska_demux_handle_seek_push: Seek end-time not supported in streaming mode"),

        # MPEG TS known issues:
        ('(?i)validate.*.playback.reverse_playback.*(?:_|.)(?:|m)ts$',
         "https://bugzilla.gnome.org/show_bug.cgi?id=702595"),

        # HTTP known issues:
        ("validate.http.*scrub_forward_seeking.*", "This is not stable enough for now."),
    ])

def gst_validate_register_defaults():
    gst_validate_register_default_test_generators()
    gst_validate_register_default_scenarios()
    gst_validate_register_default_encoding_formats()
    gst_validate_define_default_blacklist()
