#!/usr/bin/env python3
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

""" Class representing tests and test managers. """

import json
import os
import sys
import re
import copy
import socketserver
import struct
import time
from . import utils
import signal
import urllib.parse
import subprocess
import threading
import queue
from . import reporters
import configparser
from . import loggable
from .loggable import Loggable
import xml.etree.cElementTree as ET

from .utils import mkdir, Result, Colors, printc, DEFAULT_TIMEOUT, GST_SECOND, \
    Protocols, look_for_file_in_source_dir, get_data_file, BackTraceGenerator, \
    check_bugs_resolution

# The factor by which we increase the hard timeout when running inside
# Valgrind
GDB_TIMEOUT_FACTOR = VALGRIND_TIMEOUT_FACTOR = 20
TIMEOUT_FACTOR = float(os.environ.get("TIMEOUT_FACTOR", 1))
# The error reported by valgrind when detecting errors
VALGRIND_ERROR_CODE = 20

VALIDATE_OVERRIDE_EXTENSION = ".override"
COREDUMP_SIGNALS = [-signal.SIGQUIT, -signal.SIGILL, -signal.SIGABRT,
    -signal.SIGFPE, -signal.SIGSEGV, -signal.SIGBUS, -signal.SIGSYS,
    -signal.SIGTRAP, -signal.SIGXCPU, -signal.SIGXFSZ, -signal.SIGIOT,
    139]


class Test(Loggable):

    """ A class representing a particular test. """

    def __init__(self, application_name, classname, options,
                 reporter, duration=0, timeout=DEFAULT_TIMEOUT,
                 hard_timeout=None, extra_env_variables=None,
                 expected_failures=None):
        """
        @timeout: The timeout during which the value return by get_current_value
                  keeps being exactly equal
        @hard_timeout: Max time the test can take in absolute
        """
        Loggable.__init__(self)
        self.timeout = timeout * TIMEOUT_FACTOR
        if hard_timeout:
            self.hard_timeout = hard_timeout * TIMEOUT_FACTOR
        else:
            self.hard_timeout = hard_timeout
        self.classname = classname
        self.options = options
        self.application = application_name
        self.command = []
        self.reporter = reporter
        self.process = None
        self.proc_env = None
        self.thread = None
        self.queue = None
        self.duration = duration
        self.stack_trace = None
        if expected_failures is None:
            self.expected_failures = []
        elif not isinstance(expected_failures, list):
            self.expected_failures = [expected_failures]
        else:
            self.expected_failures = expected_failures

        extra_env_variables = extra_env_variables or {}
        self.extra_env_variables = extra_env_variables

        self.clean()

    def clean(self):
        self.kill_subprocess()
        self.message = ""
        self.error_str = ""
        self.time_taken = 0.0
        self._starting_time = None
        self.result = Result.NOT_RUN
        self.logfile = None
        self.out = None
        self.extra_logfiles = []
        self.__env_variable = []
        self.kill_subprocess()

    def __str__(self):
        string = self.classname
        if self.result != Result.NOT_RUN:
            string += ": " + self.result
            if self.result in [Result.FAILED, Result.TIMEOUT]:
                string += " '%s'\n" \
                          "       You can reproduce with: %s %s\n" \
                    % (self.message, self._env_variable, ' '.join(self.command))

                if not self.options.redirect_logs:
                    string += "       You can find logs in:\n" \
                              "             - %s" % (self.logfile)
                for log in self.extra_logfiles:
                    string += "\n             - %s" % log

        return string

    def add_env_variable(self, variable, value=None):
        """
        Only usefull so that the gst-validate-launcher can print the exact
        right command line to reproduce the tests
        """
        if value is None:
            value = os.environ.get(variable, None)

        if value is None:
            return

        self.__env_variable.append(variable)

    @property
    def _env_variable(self):
        res = ""
        for var in set(self.__env_variable):
            if res:
                res += " "
            value = self.proc_env.get(var, None)
            if value:
                res += "%s='%s'" % (var, value)

        return res

    def open_logfile(self):
        if self.out:
            return

        path = os.path.join(self.options.logsdir,
                            self.classname.replace(".", os.sep))
        mkdir(os.path.dirname(path))
        self.logfile = path

        if self.options.redirect_logs == 'stdout':
            self.out = sys.stdout
        elif self.options.redirect_logs == 'stderr':
            self.out = sys.stderr
        else:
            self.out = open(path, 'w+')

    def close_logfile(self):
        if not self.options.redirect_logs:
            self.out.close()

        self.out = None

    def _get_file_content(self, file_name):
        f = open(file_name, 'r+')
        value = f.read()
        f.close()

        return value

    def get_log_content(self):
        return self._get_file_content(self.logfile)

    def get_extra_log_content(self, extralog):
        if extralog not in self.extra_logfiles:
            return ""

        return self._get_file_content(extralog)

    def get_classname(self):
        name = self.classname.split('.')[-1]
        classname = self.classname.replace('.%s' % name, '')

        return classname

    def get_name(self):
        return self.classname.split('.')[-1]

    def add_arguments(self, *args):
        self.command += args

    def build_arguments(self):
        self.add_env_variable("LD_PRELOAD")
        self.add_env_variable("DISPLAY")

    def add_stack_trace_to_logfile(self):
        trace_gatherer = BackTraceGenerator.get_default()
        stack_trace = trace_gatherer.get_trace(self)

        if not stack_trace:
            return

        info = "\n\n== Stack trace: == \n%s" % stack_trace
        if self.options.redirect_logs:
            print(info)
        elif self.options.xunit_file:
            self.stack_trace = stack_trace
        else:
            with open(self.logfile, 'a') as f:
                f.write(info)

    def set_result(self, result, message="", error=""):
        self.debug("Setting result: %s (message: %s, error: %s)" % (result,
                   message, error))

        if result is Result.TIMEOUT:
            if self.options.debug is True:
                if self.options.gdb:
                    printc("Timeout, you should process <ctrl>c to get into gdb",
                        Colors.FAIL)
                    # and wait here until gdb exits
                    self.process.communicate()
                else:
                    pname = subprocess.check_output(("readlink -e /proc/%s/exe"
                                                    % self.process.pid).decode().split(' ')).replace('\n', '')
                    input("%sTimeout happened you can attach gdb doing: $gdb %s %d%s\n"
                          "Press enter to continue" % (Colors.FAIL, pname, self.process.pid,
                                                       Colors.ENDC))
            else:
                self.add_stack_trace_to_logfile()

        self.result = result
        self.message = message
        self.error_str = error

    def check_results(self):
        if self.result is Result.FAILED or self.result is Result.TIMEOUT:
            return

        self.debug("%s returncode: %s", self, self.process.returncode)
        if self.process.returncode == 0:
            self.set_result(Result.PASSED)
        elif self.process.returncode in [-signal.SIGSEGV, -signal.SIGABRT, 139]:
            self.add_stack_trace_to_file()
            self.set_result(Result.FAILED,
                            "Application segfaulted, returne code: %d" % (
                                self.process.returncode))
        elif self.process.returncode == VALGRIND_ERROR_CODE:
            self.set_result(Result.FAILED, "Valgrind reported errors")
        else:
            self.set_result(Result.FAILED,
                            "Application returned %d" % (self.process.returncode))

    def get_current_value(self):
        """
        Lets subclasses implement a nicer timeout measurement method
        They should return some value with which we will compare
        the previous and timeout if they are egual during self.timeout
        seconds
        """
        return Result.NOT_RUN

    def process_update(self):
        """
        Returns True when process has finished running or has timed out.
        """

        if self.process is None:
            # Process has not started running yet
            return False

        self.process.poll()
        if self.process.returncode is not None:
            return True

        val = self.get_current_value()

        self.debug("Got value: %s" % val)
        if val is Result.NOT_RUN:
            # The get_current_value logic is not implemented... dumb
            # timeout
            if time.time() - self.last_change_ts > self.timeout:
                self.set_result(Result.TIMEOUT,
                                "Application timed out: %s secs" %
                                self.timeout,
                                "timeout")
                return True
            return False
        elif val is Result.FAILED:
            return True
        elif val is Result.KNOWN_ERROR:
            return True

        self.log("New val %s" % val)

        if val == self.last_val:
            delta = time.time() - self.last_change_ts
            self.debug("%s: Same value for %d/%d seconds" %
                       (self, delta, self.timeout))
            if delta > self.timeout:
                self.set_result(Result.TIMEOUT,
                                "Application timed out: %s secs" %
                                self.timeout,
                                "timeout")
                return True
        elif self.hard_timeout and time.time() - self.start_ts > self.hard_timeout:
            self.set_result(
                Result.TIMEOUT, "Hard timeout reached: %d secs" % self.hard_timeout)
            return True
        else:
            self.last_change_ts = time.time()
            self.last_val = val

        return False

    def get_subproc_env(self):
        return os.environ

    def kill_subprocess(self):
        if self.process is None:
            return

        stime = time.time()
        res = self.process.poll()
        while res is None:
            try:
                self.debug("Subprocess is still alive, sending KILL signal")
                self.process.send_signal(signal.SIGKILL)
                time.sleep(1)
            except OSError:
                pass
            if time.time() - stime > DEFAULT_TIMEOUT:
                raise RuntimeError("Could not kill subprocess after %s second"
                                   " Something is really wrong, => EXITING"
                                   % DEFAULT_TIMEOUT)
            res = self.process.poll()

    def thread_wrapper(self):
        self.process = subprocess.Popen(self.command,
                                        stderr=self.out,
                                        stdout=self.out,
                                        env=self.proc_env)
        self.process.wait()
        if self.result is not Result.TIMEOUT:
            self.queue.put(None)

    def get_valgrind_suppression_file(self, subdir, name):
        p = get_data_file(subdir, name)
        if p:
            return p

        self.error("Could not find any %s file" % name)

    def get_valgrind_suppressions(self):
        return [self.get_valgrind_suppression_file('data', 'gstvalidate.supp')]

    def use_gdb(self):
        if self.hard_timeout is not None:
            self.hard_timeout *= GDB_TIMEOUT_FACTOR
        self.timeout *= GDB_TIMEOUT_FACTOR
        self.command = ["gdb", "-ex", "run", "-ex", "quit",
                        "--args"] + self.command

    def use_valgrind(self):
        vglogsfile = self.logfile + '.valgrind'
        self.extra_logfiles.append(vglogsfile)

        vg_args = []

        for o, v in [('trace-children', 'yes'),
                ('tool', 'memcheck'),
                ('leak-check', 'full'),
                ('leak-resolution', 'high'),
                # TODO: errors-for-leak-kinds should be set to all instead of definite
                #       and all false positives should be added to suppression files.
                ('errors-for-leak-kinds', 'definite'),
                ('num-callers', '20'),
                ('log-file', vglogsfile),
                ('error-exitcode', str(VALGRIND_ERROR_CODE)),
                ('gen-suppressions', 'all')]:
            vg_args.append("--%s=%s" % (o, v))

        for supp in self.get_valgrind_suppressions():
            vg_args.append("--suppressions=%s" % supp)

        self.command = ["valgrind"] + vg_args + self.command

        # Tune GLib's memory allocator to be more valgrind friendly
        self.proc_env['G_DEBUG'] = 'gc-friendly'
        self.add_env_variable('G_DEBUG', 'gc-friendly')

        self.proc_env['G_SLICE'] = 'always-malloc'
        self.add_env_variable('G_SLICE', 'always-malloc')

        if self.hard_timeout is not None:
            self.hard_timeout *= VALGRIND_TIMEOUT_FACTOR
        self.timeout *= VALGRIND_TIMEOUT_FACTOR

        # Enable 'valgrind.config'
        vg_config = get_data_file('data', 'valgrind.config')

        if self.proc_env.get('GST_VALIDATE_CONFIG'):
            self.proc_env['GST_VALIDATE_CONFIG'] = '%s%s%s' % (self.proc_env['GST_VALIDATE_CONFIG'], os.pathsep, vg_config)
        else:
            self.proc_env['GST_VALIDATE_CONFIG'] = vg_config

        self.add_env_variable('GST_VALIDATE_CONFIG', self.proc_env['GST_VALIDATE_CONFIG'])

    def test_start(self, queue):
        self.open_logfile()

        self.queue = queue
        self.command = [self.application]
        self._starting_time = time.time()
        self.build_arguments()
        self.proc_env = self.get_subproc_env()

        for var, value in list(self.extra_env_variables.items()):
            value = self.proc_env.get(var, '') + os.pathsep + value
            self.proc_env[var] = value.strip(os.pathsep)
            self.add_env_variable(var, self.proc_env[var])

        if self.options.gdb:
            self.use_gdb()

        if self.options.valgrind:
            self.use_valgrind()

        message = "Launching: %s%s\n" \
                  "    Command: '%s %s'\n" % (Colors.ENDC, self.classname,
                                              self._env_variable, ' '.join(self.command))
        if not self.options.redirect_logs:
            message += "    Logs:\n" \
                       "         - %s" % (self.logfile)
            for log in self.extra_logfiles:
                message += "\n         - %s" % log

            self.out.write("=================\n"
                           "Test name: %s\n"
                           "Command: '%s'\n"
                           "=================\n\n"
                           % (self.classname, ' '.join(self.command)))
            self.out.flush()

        printc(message, Colors.OKBLUE)

        self.thread = threading.Thread(target=self.thread_wrapper)
        self.thread.start()

        self.last_val = 0
        self.last_change_ts = time.time()
        self.start_ts = time.time()

    def _dump_log_file(self, logfile):
        message = "Dumping contents of %s\n" % logfile
        printc(message, Colors.FAIL)

        with open(logfile, 'r') as fin:
            print(fin.read())

    def _dump_log_files(self):
        printc("Dumping log files on failure\n", Colors.FAIL)
        self._dump_log_file(self.logfile)
        for logfile in self.extra_logfiles:
            self._dump_log_file(logfile)

    def test_end(self):
        self.kill_subprocess()
        self.thread.join()
        self.time_taken = time.time() - self._starting_time

        printc("%s: %s%s\n" % (self.classname, self.result,
               " (" + self.message + ")" if self.message else ""),
               color=utils.get_color_for_result(self.result))

        self.close_logfile()

        if self.options.dump_on_failure:
            if self.result is not Result.PASSED:
                self._dump_log_files()

        return self.result


class GstValidateListener(socketserver.BaseRequestHandler):
    def handle(self):
        """Implements BaseRequestHandler handle method"""
        while True:
            raw_len = self.request.recv(4)
            if raw_len == b'':
                return
            msglen = struct.unpack('>I', raw_len)[0]
            msg = self.request.recv(msglen).decode()
            if msg == '':
                return

            obj = json.loads(msg)
            test = getattr(self.server, "test")

            obj_type = obj.get("type", '')
            if obj_type == 'position':
                test.set_position(obj['position'], obj['duration'],
                                obj['speed'])
            elif obj_type == 'buffering':
                test.set_position(obj['position'], 100)
            elif obj_type == 'action':
                test.add_action_execution(obj)
            elif obj_type == 'report':
                test.add_report(obj)


class GstValidateTest(Test):

    """ A class representing a particular test. """
    findpos_regex = re.compile(
        '.*position.*(\d+):(\d+):(\d+).(\d+).*duration.*(\d+):(\d+):(\d+).(\d+)')
    findlastseek_regex = re.compile(
        'seeking to.*(\d+):(\d+):(\d+).(\d+).*stop.*(\d+):(\d+):(\d+).(\d+).*rate.*(\d+)\.(\d+)')

    HARD_TIMEOUT_FACTOR = 5

    def __init__(self, application_name, classname,
                 options, reporter, duration=0,
                 timeout=DEFAULT_TIMEOUT, scenario=None, hard_timeout=None,
                 media_descriptor=None, extra_env_variables=None,
                 expected_failures=None):

        extra_env_variables = extra_env_variables or {}

        if not hard_timeout and self.HARD_TIMEOUT_FACTOR:
            if timeout:
                hard_timeout = timeout * self.HARD_TIMEOUT_FACTOR
            elif duration:
                hard_timeout = duration * self.HARD_TIMEOUT_FACTOR
            else:
                hard_timeout = None

        # If we are running from source, use the -debug version of the
        # application which is using rpath instead of libtool's wrappers. It's
        # slightly faster to start and will not confuse valgrind.
        debug = '%s-debug' % application_name
        p = look_for_file_in_source_dir('tools', debug)
        if p:
            application_name = p

        self.reports = []
        self.position = -1
        self.media_duration = -1
        self.speed = 1.0
        self.actions_infos = []
        self.media_descriptor = media_descriptor
        self.server = None

        override_path = self.get_override_file(media_descriptor)
        if override_path:
            if extra_env_variables:
                if extra_env_variables.get("GST_VALIDATE_OVERRIDE", ""):
                    extra_env_variables["GST_VALIDATE_OVERRIDE"] += os.path.pathsep

            extra_env_variables["GST_VALIDATE_OVERRIDE"] = override_path

        super(GstValidateTest, self).__init__(application_name, classname,
                                              options, reporter,
                                              duration=duration,
                                              timeout=timeout,
                                              hard_timeout=hard_timeout,
                                              extra_env_variables=extra_env_variables,
                                              expected_failures=expected_failures)

        # defines how much the process can be outside of the configured
        # segment / seek
        self._sent_eos_time = None

        if scenario is None or scenario.name.lower() == "none":
            self.scenario = None
        else:
            self.scenario = scenario

    def stop_server(self):
        if self.server:
            self.server.shutdown()
            self.server_thread.join()
            self.server.server_close()
            self.server = None

    def kill_subprocess(self):
        Test.kill_subprocess(self)
        self.stop_server()

    def add_report(self, report):
        self.reports.append(report)

    def set_position(self, position, duration, speed=None):
        self.position = position
        self.media_duration = duration
        if speed:
            self.speed = speed

    def add_action_execution(self, action_infos):
        if action_infos['action-type'] == 'eos':
            self._sent_eos_time = time.time()
        self.actions_infos.append(action_infos)

    def server_wrapper(self, ready):
        self.server = socketserver.TCPServer(('localhost', 0), GstValidateListener)
        self.server.socket.settimeout(0.0)
        self.server.test = self
        self.serverport = self.server.socket.getsockname()[1]
        self.info("%s server port: %s" % (self, self.serverport))
        ready.set()

        self.server.serve_forever()

    def test_start(self, queue):
        ready = threading.Event()
        self.server_thread = threading.Thread(target=self.server_wrapper,
                                              kwargs={'ready': ready})
        self.server_thread.start()
        ready.wait()

        Test.test_start(self, queue)

    def test_end(self):
        res = Test.test_end(self)
        self.stop_server()

        return res

    def get_override_file(self, media_descriptor):
        if media_descriptor:
            if media_descriptor.get_path():
                override_path = os.path.splitext(media_descriptor.get_path())[0] + VALIDATE_OVERRIDE_EXTENSION
                if os.path.exists(override_path):
                    return override_path

        return None

    def get_current_position(self):
        return self.position

    def get_current_value(self):
        if self.scenario:
            if self._sent_eos_time is not None:
                t = time.time()
                if ((t - self._sent_eos_time)) > 30:
                    if self.media_descriptor.get_protocol() == Protocols.HLS:
                        self.set_result(Result.PASSED,
                                        """Got no EOS 30 seconds after sending EOS,
                                        in HLS known and tolerated issue:
                                        https://bugzilla.gnome.org/show_bug.cgi?id=723868""")
                        return Result.KNOWN_ERROR

                    self.set_result(
                        Result.FAILED, "Pipeline did not stop 30 Seconds after sending EOS")

                    return Result.FAILED

        return self.position

    def get_subproc_env(self):
        subproc_env = os.environ.copy()

        subproc_env["GST_VALIDATE_SERVER"] = "tcp://localhost:%s" % self.serverport

        if 'GST_DEBUG' in os.environ and not self.options.redirect_logs:
            gstlogsfile = self.logfile + '.gstdebug'
            self.extra_logfiles.append(gstlogsfile)
            subproc_env["GST_DEBUG_FILE"] = gstlogsfile

        if self.options.no_color:
            subproc_env["GST_DEBUG_NO_COLOR"] = '1'

        # Ensure XInitThreads is called, see bgo#731525
        subproc_env['GST_GL_XINITTHREADS'] = '1'
        self.add_env_variable('GST_GL_XINITTHREADS', '1')

        if self.scenario is not None:
            scenario = self.scenario.get_execution_name()
            if self.options.valgrind:
                # Increase sink's max-lateness property when running inside
                # Valgrind as it slows down everything quiet a lot.
                scenario = "setup_sink_props_max_lateness:%s" % scenario

            subproc_env["GST_VALIDATE_SCENARIO"] = scenario
            self.add_env_variable("GST_VALIDATE_SCENARIO",
                                  subproc_env["GST_VALIDATE_SCENARIO"])
        else:
            try:
                del subproc_env["GST_VALIDATE_SCENARIO"]
            except KeyError:
                pass

        return subproc_env

    def clean(self):
        Test.clean(self)
        self._sent_eos_time = None
        self.reports = []
        self.position = -1
        self.media_duration = -1
        self.speed = 1.0
        self.actions_infos = []

    def build_arguments(self):
        super(GstValidateTest, self).build_arguments()
        if "GST_VALIDATE" in os.environ:
            self.add_env_variable("GST_VALIDATE", os.environ["GST_VALIDATE"])

        if "GST_VALIDATE_SCENARIOS_PATH" in os.environ:
            self.add_env_variable("GST_VALIDATE_SCENARIOS_PATH",
                                  os.environ["GST_VALIDATE_SCENARIOS_PATH"])

        self.add_env_variable("GST_VALIDATE_CONFIG")
        self.add_env_variable("GST_VALIDATE_OVERRIDE")

    def get_extra_log_content(self, extralog):
        value = Test.get_extra_log_content(self, extralog)

        return value

    def report_matches_expected_failure(self, report, expected_failure):
        for key in ['bug', 'bugs', 'sometimes']:
            if key in expected_failure:
                del expected_failure[key]
        for key, value in list(report.items()):
            if key in expected_failure:
                if not re.findall(expected_failure[key], str(value)):
                    return False
                expected_failure.pop(key)

        return not bool(expected_failure)

    def check_reported_issues(self):
        ret = []
        expected_failures = copy.deepcopy(self.expected_failures)
        expected_retcode = [0]
        for report in self.reports:
            found = None
            for expected_failure in expected_failures:
                if self.report_matches_expected_failure(report,
                                                        expected_failure.copy()):
                    found = expected_failure
                    break

            if found is not None:
                expected_failures.remove(found)
                if report['level'] == 'critical':
                    if found.get('sometimes') and isinstance(expected_retcode, list):
                        expected_retcode.append(18)
                    else:
                        expected_retcode = [18]
            elif report['level'] == 'critical':
                ret.append(report['summary'])

        if not ret:
            return None, expected_failures, expected_retcode

        return ret, expected_failures, expected_retcode

    def check_expected_timeout(self, expected_timeout):
        msg = "Expected timeout happened. "
        result = Result.PASSED
        message = expected_timeout.get('message')
        if message:
            if not re.findall(message, self.message):
                result = Result.FAILED
                msg = "Expected timeout message: %s got %s " % (
                    message, self.message)

        expected_symbols = expected_timeout.get('stacktrace_symbols')
        if expected_symbols:
            trace_gatherer = BackTraceGenerator.get_default()
            stack_trace = trace_gatherer.get_trace(self)

            if stack_trace:
                if not isinstance(expected_symbols, list):
                    expected_symbols = [expected_symbols]

                not_found_symbols = [s for s in expected_symbols
                                     if s not in stack_trace]
                if not_found_symbols:
                    result = Result.TIMEOUT
                    msg = "Expected symbols '%s' not found in stack trace " % (
                        not_found_symbols)
            else:
                msg += "No stack trace available, could not verify symbols "

        return result, msg

    def check_results(self):
        if self.result in [Result.FAILED, self.result is Result.PASSED]:
            return

        self.debug("%s returncode: %s", self, self.process.returncode)

        criticals, not_found_expected_failures, expected_returncode = self.check_reported_issues()

        expected_timeout = None
        for i, f in enumerate(not_found_expected_failures):
            if len(f) == 1 and f.get("returncode"):
                returncode = f['returncode']
                if not isinstance(expected_returncode, list):
                    returncode = [expected_returncode]
                if 'sometimes' in f:
                    returncode.append(0)
            elif f.get("timeout"):
                expected_timeout = f

        not_found_expected_failures = [f for f in not_found_expected_failures
                                       if not f.get('returncode')]

        msg = ""
        result = Result.PASSED
        if self.result == Result.TIMEOUT:
            if expected_timeout:
                not_found_expected_failures.remove(expected_timeout)
                result, msg = self.check_expected_timeout(expected_timeout)
            else:
                return
        elif self.process.returncode in COREDUMP_SIGNALS:
            result = Result.FAILED
            msg = "Application segfaulted "
            self.add_stack_trace_to_logfile()
        elif self.process.returncode == VALGRIND_ERROR_CODE:
            msg = "Valgrind reported errors "
            result = Result.FAILED
        elif self.process.returncode not in expected_returncode:
            msg = "Application returned %s " % self.process.returncode
            if expected_returncode != [0]:
                msg += "(expected %s) " % expected_returncode
            result = Result.FAILED

        if criticals:
            msg += "(critical errors: [%s]) " % ', '.join(criticals)
            result = Result.FAILED

        if not_found_expected_failures:
            mandatory_failures = [f for f in not_found_expected_failures
                                  if not f.get('sometimes')]

            if mandatory_failures:
                msg += "(Expected errors not found: %s) " % mandatory_failures
                result = Result.FAILED
        elif self.expected_failures:
                msg += '%s(Expected errors occured: %s)%s' % (Colors.OKBLUE,
                                                           self.expected_failures,
                                                           Colors.ENDC)

        self.set_result(result, msg.strip())

    def get_valgrind_suppressions(self):
        result = super(GstValidateTest, self).get_valgrind_suppressions()
        return result + [self.get_valgrind_suppression_file('common', 'gst.supp')]


class GstValidateEncodingTestInterface(object):
    DURATION_TOLERANCE = GST_SECOND / 4

    def __init__(self, combination, media_descriptor, duration_tolerance=None):
        super(GstValidateEncodingTestInterface, self).__init__()

        self.media_descriptor = media_descriptor
        self.combination = combination
        self.dest_file = ""

        self._duration_tolerance = duration_tolerance
        if duration_tolerance is None:
            self._duration_tolerance = self.DURATION_TOLERANCE

    def get_current_size(self):
        try:
            size = os.stat(urllib.parse.urlparse(self.dest_file).path).st_size
        except OSError:
            return None

        self.debug("Size: %s" % size)
        return size

    def _get_profile_full(self, muxer, venc, aenc, video_restriction=None,
                          audio_restriction=None, audio_presence=0,
                          video_presence=0):
        ret = ""
        if muxer:
            ret += muxer
        ret += ":"
        if venc:
            if video_restriction is not None:
                ret = ret + video_restriction + '->'
            ret += venc
            if video_presence:
                ret = ret + '|' + str(video_presence)
        if aenc:
            ret += ":"
            if audio_restriction is not None:
                ret = ret + audio_restriction + '->'
            ret += aenc
            if audio_presence:
                ret = ret + '|' + str(audio_presence)

        return ret.replace("::", ":")

    def get_profile(self, video_restriction=None, audio_restriction=None):
        vcaps = self.combination.get_video_caps()
        acaps = self.combination.get_audio_caps()
        if self.media_descriptor is not None:
            if self.media_descriptor.get_num_tracks("video") == 0:
                vcaps = None

            if self.media_descriptor.get_num_tracks("audio") == 0:
                acaps = None

        return self._get_profile_full(self.combination.get_muxer_caps(),
                                      vcaps, acaps,
                                      video_restriction=video_restriction,
                                      audio_restriction=audio_restriction)

    def _clean_caps(self, caps):
        """
        Returns a list of key=value or structure name, without "(types)" or ";" or ","
        """
        return re.sub(r"\(.+?\)\s*| |;", '', caps).split(',')

    def _has_caps_type_variant(self, c, ccaps):
        """
        Handle situations where we can have application/ogg or video/ogg or
        audio/ogg
        """
        has_variant = False
        media_type = re.findall("application/|video/|audio/", c)
        if media_type:
            media_type = media_type[0].replace('/', '')
            possible_mtypes = ["application", "video", "audio"]
            possible_mtypes.remove(media_type)
            for tmptype in possible_mtypes:
                possible_c_variant = c.replace(media_type, tmptype)
                if possible_c_variant in ccaps:
                    self.info(
                        "Found %s in %s, good enough!", possible_c_variant, ccaps)
                    has_variant = True

        return has_variant

    def check_encoded_file(self):
        result_descriptor = GstValidateMediaDescriptor.new_from_uri(
            self.dest_file)
        if result_descriptor is None:
            return (Result.FAILED, "Could not discover encoded file %s"
                    % self.dest_file)

        duration = result_descriptor.get_duration()
        orig_duration = self.media_descriptor.get_duration()
        tolerance = self._duration_tolerance

        if orig_duration - tolerance >= duration <= orig_duration + tolerance:
            os.remove(result_descriptor.get_path())
            return (Result.FAILED, "Duration of encoded file is "
                    " wrong (%s instead of %s)" %
                    (utils.TIME_ARGS(duration),
                     utils.TIME_ARGS(orig_duration)))
        else:
            all_tracks_caps = result_descriptor.get_tracks_caps()
            container_caps = result_descriptor.get_caps()
            if container_caps:
                all_tracks_caps.insert(0, ("container", container_caps))

            for track_type, caps in all_tracks_caps:
                ccaps = self._clean_caps(caps)
                wanted_caps = self.combination.get_caps(track_type)
                cwanted_caps = self._clean_caps(wanted_caps)

                if wanted_caps is None:
                    os.remove(result_descriptor.get_path())
                    return (Result.FAILED,
                            "Found a track of type %s in the encoded files"
                            " but none where wanted in the encoded profile: %s"
                            % (track_type, self.combination))

                for c in cwanted_caps:
                    if c not in ccaps:
                        if not self._has_caps_type_variant(c, ccaps):
                            os.remove(result_descriptor.get_path())
                            return (Result.FAILED,
                                    "Field: %s  (from %s) not in caps of the outputed file %s"
                                    % (wanted_caps, c, ccaps))

            os.remove(result_descriptor.get_path())
            return (Result.PASSED, "")


class TestsManager(Loggable):

    """ A class responsible for managing tests. """

    name = ""

    def __init__(self):

        Loggable.__init__(self)

        self.tests = []
        self.unwanted_tests = []
        self.options = None
        self.args = None
        self.reporter = None
        self.wanted_tests_patterns = []
        self.blacklisted_tests_patterns = []
        self._generators = []
        self.queue = queue.Queue()
        self.jobs = []
        self.total_num_tests = 0
        self.starting_test_num = 0
        self.check_testslist = True
        self.all_tests = None
        self.expected_failures = {}
        self.blacklisted_tests = []

    def init(self):
        return False

    def list_tests(self):
        return sorted(list(self.tests))

    def add_expected_issues(self, expected_failures):
        expected_failures_re = {}
        for test_name_regex, failures in list(expected_failures.items()):
            regex = re.compile(test_name_regex)
            expected_failures_re[regex] = failures
            for test in self.tests:
                if regex.findall(test.classname):
                    test.expected_failures.extend(failures)

        self.expected_failures.update(expected_failures_re)

    def add_test(self, test):
        for regex, failures in list(self.expected_failures.items()):
            if regex.findall(test.classname):
                test.expected_failures.extend(failures)

        if self._is_test_wanted(test):
            if test not in self.tests:
                self.tests.append(test)
                self.tests.sort(key=lambda test: test.classname)
        else:
            if test not in self.tests:
                self.unwanted_tests.append(test)
                self.unwanted_tests.sort(key=lambda test: test.classname)

    def get_tests(self):
        return self.tests

    def populate_testsuite(self):
        pass

    def add_generators(self, generators):
        """
        @generators: A list of, or one single #TestsGenerator to be used to generate tests
        """
        if isinstance(generators, list):
            self._generators.extend(generators)
        else:
            self._generators.append(generators)

        self._generators = list(set(self._generators))

    def get_generators(self):
        return self._generators

    def _add_blacklist(self, blacklisted_tests):
        if not isinstance(blacklisted_tests, list):
            blacklisted_tests = [blacklisted_tests]

        for patterns in blacklisted_tests:
            for pattern in patterns.split(","):
                self.blacklisted_tests_patterns.append(re.compile(pattern))

    def set_default_blacklist(self, default_blacklist):
        self.blacklisted_tests += default_blacklist

    def add_options(self, parser):
        """ Add more arguments. """
        pass

    def set_settings(self, options, args, reporter):
        """ Set properties after options parsing. """
        self.options = options
        self.args = args
        self.reporter = reporter

        self.populate_testsuite()

        if self.options.valgrind:
            self.print_valgrind_bugs()

        if options.wanted_tests:
            for patterns in options.wanted_tests:
                for pattern in patterns.split(","):
                    self.wanted_tests_patterns.append(re.compile(pattern))

        if options.blacklisted_tests:
            for patterns in options.blacklisted_tests:
                self._add_blacklist(patterns)

    def set_blacklists(self):
        if self.blacklisted_tests:
            printc("\nCurrently 'hardcoded' %s blacklisted tests:" %
                   self.name, Colors.WARNING, title_char='-')

        if self.options.check_bugs_status:
            if not check_bugs_resolution(self.blacklisted_tests):
                return False

        for name, bug in self.blacklisted_tests:
            self._add_blacklist(name)
            if not self.options.check_bugs_status:
                print("  + %s \n   --> bug: %s\n" % (name, bug))

        return True

    def check_expected_failures(self):
        if not self.expected_failures or not self.options.check_bugs_status:
            return True

        if self.expected_failures:
            printc("\nCurrently known failures in the %s testsuite:"
                   % self.name, Colors.WARNING, title_char='-')

        bugs_definitions = {}
        for regex, failures in list(self.expected_failures.items()):
            for failure in failures:
                bugs = failure.get('bug')
                if not bugs:
                    bugs = failure.get('bugs')
                if not bugs:
                    printc('+ %s:\n  --> no bug reported associated with %s\n' % (
                        regex.pattern, failure), Colors.WARNING)
                    continue

                if not isinstance(bugs, list):
                    bugs = [bugs]
                cbugs = bugs_definitions.get(regex.pattern, [])
                bugs.extend([b for b in bugs if b not in cbugs])
                bugs_definitions[regex.pattern] = bugs

        return check_bugs_resolution(bugs_definitions.items())

    def _check_blacklisted(self, test):
        for pattern in self.blacklisted_tests_patterns:
            if pattern.findall(test.classname):
                self.info("%s is blacklisted by %s", test.classname, pattern)
                return True

        return False

    def _check_whitelisted(self, test):
        for pattern in self.wanted_tests_patterns:
            if pattern.findall(test.classname):
                if self._check_blacklisted(test):
                    # If explicitly white listed that specific test
                    # bypass the blacklisting
                    if pattern.pattern != test.classname:
                        return False
                return True
        return False

    def _check_duration(self, test):
        if test.duration > 0 and int(self.options.long_limit) < int(test.duration):
            self.info("Not activating %s as its duration (%d) is superior"
                      " than the long limit (%d)" % (test, test.duration,
                                                     int(self.options.long_limit)))
            return False

        return True

    def _is_test_wanted(self, test):
        if self._check_whitelisted(test):
            if not self._check_duration(test):
                return False
            return True

        if self._check_blacklisted(test):
            return False

        if not self._check_duration(test):
            return False

        if not self.wanted_tests_patterns:
            return True

        return False

    def test_wait(self):
        while True:
            # Check process every second for timeout
            try:
                self.queue.get(timeout=1)
            except queue.Empty:
                pass

            for test in self.jobs:
                if test.process_update():
                    self.jobs.remove(test)
                    return test

    def tests_wait(self):
        try:
            test = self.test_wait()
            test.check_results()
        except KeyboardInterrupt:
            for test in self.jobs:
                test.kill_subprocess()
            raise

        return test

    def start_new_job(self, tests_left):
        try:
            test = tests_left.pop(0)
        except IndexError:
            return False

        self.print_test_num(test)
        test.test_start(self.queue)

        self.jobs.append(test)

        return True

    def run_tests(self, starting_test_num, total_num_tests):
        self.total_num_tests = total_num_tests
        self.starting_test_num = starting_test_num

        num_jobs = min(self.options.num_jobs, len(self.tests))
        tests_left = list(self.tests)
        jobs_running = 0

        for i in range(num_jobs):
            if not self.start_new_job(tests_left):
                break
            jobs_running += 1

        while jobs_running != 0:
            test = self.tests_wait()
            jobs_running -= 1
            self.print_test_num(test)
            res = test.test_end()
            self.reporter.after_test(test)
            if res != Result.PASSED and (self.options.forever or
                                         self.options.fatal_error):
                return test.result
            if self.start_new_job(tests_left):
                jobs_running += 1

        return Result.PASSED

    def print_test_num(self, test):
        cur_test_num = self.starting_test_num + self.tests.index(test) + 1
        sys.stdout.write("[%d / %d] " % (cur_test_num, self.total_num_tests))

    def clean_tests(self):
        for test in self.tests:
            test.clean()

    def needs_http_server(self):
        return False

    def print_valgrind_bugs(self):
        pass


class TestsGenerator(Loggable):

    def __init__(self, name, test_manager, tests=[]):
        Loggable.__init__(self)
        self.name = name
        self.test_manager = test_manager
        self._tests = {}
        for test in tests:
            self._tests[test.classname] = test

    def generate_tests(self, *kwargs):
        """
        Method that generates tests
        """
        return list(self._tests.values())

    def add_test(self, test):
        self._tests[test.classname] = test


class GstValidateTestsGenerator(TestsGenerator):

    def populate_tests(self, uri_minfo_special_scenarios, scenarios):
        pass

    def generate_tests(self, uri_minfo_special_scenarios, scenarios):
        self.populate_tests(uri_minfo_special_scenarios, scenarios)
        return super(GstValidateTestsGenerator, self).generate_tests()


class _TestsLauncher(Loggable):

    def __init__(self, libsdir):

        Loggable.__init__(self)

        self.libsdir = libsdir
        self.options = None
        self.testers = []
        self.tests = []
        self.reporter = None
        self._list_testers()
        self.all_tests = None
        self.wanted_tests_patterns = []

    def _list_app_dirs(self):
        app_dirs = []
        app_dirs.append(os.path.join(self.libsdir, "apps"))
        env_dirs = os.environ.get("GST_VALIDATE_APPS_DIR")
        if env_dirs is not None:
            for dir_ in env_dirs.split(":"):
                app_dirs.append(dir_)
                sys.path.append(dir_)

        return app_dirs

    def _exec_app(self, app_dir, env):
        try:
            files = os.listdir(app_dir)
        except OSError as e:
            self.debug("Could not list %s: %s" % (app_dir, e))
            files = []
        for f in files:
            if f.endswith(".py"):
                exec(compile(open(os.path.join(app_dir, f)).read(), os.path.join(app_dir, f), 'exec'), env)

    def _exec_apps(self, env):
        app_dirs = self._list_app_dirs()
        for app_dir in app_dirs:
            self._exec_app(app_dir, env)

    def _list_testers(self):
        env = globals().copy()
        self._exec_apps(env)

        testers = [i() for i in utils.get_subclasses(TestsManager, env)]
        for tester in testers:
            if tester.init() is True:
                self.testers.append(tester)
            else:
                self.warning("Can not init tester: %s -- PATH is %s"
                             % (tester.name, os.environ["PATH"]))

    def add_options(self, parser):
        for tester in self.testers:
            tester.add_options(parser)

    def _load_testsuite(self, testsuites):
        exceptions = []
        for testsuite in testsuites:
            try:
                sys.path.insert(0, os.path.dirname(testsuite))
                return (__import__(os.path.basename(testsuite).replace(".py", "")), None)
            except Exception as e:
                exceptions.append("Could not load %s: %s" % (testsuite, e))
                continue
            finally:
                sys.path.remove(os.path.dirname(testsuite))

        return (None, exceptions)

    def _load_testsuites(self):
        testsuites = []
        for testsuite in self.options.testsuites:
            if os.path.isabs(testsuite):
                loaded_module = self._load_testsuite([testsuite])
            else:
                possible_testsuites_paths = [os.path.join(d, testsuite + ".py")
                              for d in self.options.testsuites_dirs]
                loaded_module = self._load_testsuite(possible_testsuites_paths)

            module = loaded_module[0]
            if not loaded_module[0]:
                printc("Could not load testsuite: %s, reasons: %s" % (
                    testsuite, loaded_module[1]), Colors.FAIL)
                continue

            testsuites.append(module)
            if not hasattr(module, "TEST_MANAGER"):
                module.TEST_MANAGER = [tester.name for tester in self.testers]
            elif not isinstance(module.TEST_MANAGER, list):
                module.TEST_MANAGER = [module.TEST_MANAGER]

        self.options.testsuites = testsuites

    def _setup_testsuites(self):
        for testsuite in self.options.testsuites:
            loaded = False
            wanted_test_manager = None
            if hasattr(testsuite, "TEST_MANAGER"):
                wanted_test_manager = testsuite.TEST_MANAGER
                if not isinstance(wanted_test_manager, list):
                    wanted_test_manager = [wanted_test_manager]

            for tester in self.testers:
                if wanted_test_manager is not None and \
                        tester.name not in wanted_test_manager:
                    continue

                if self.options.user_paths:
                    tester.register_defaults()
                    loaded = True
                elif testsuite.setup_tests(tester, self.options):
                    loaded = True

            if not loaded:
                printc("Could not load testsuite: %s"
                       " maybe because of missing TestManager"
                       % (testsuite), Colors.FAIL)
                return False

    def _load_config(self, options):
        printc("Loading config files is DEPRECATED"
               " you should use the new testsuite format now",)

        for tester in self.testers:
            tester.options = options
            globals()[tester.name] = tester
        globals()["options"] = options
        c__file__ = __file__
        globals()["__file__"] = self.options.config
        exec(compile(open(self.options.config).read(), self.options.config, 'exec'), globals())
        globals()["__file__"] = c__file__

    def set_settings(self, options, args):
        if options.xunit_file:
            self.reporter = reporters.XunitReporter(options)
        else:
            self.reporter = reporters.Reporter(options)

        self.options = options
        wanted_testers = None
        for tester in self.testers:
            if tester.name in args:
                wanted_testers = tester.name

        if wanted_testers:
            testers = self.testers
            self.testers = []
            for tester in testers:
                if tester.name in args:
                    self.testers.append(tester)
                    args.remove(tester.name)

        if options.config:
            self._load_config(options)

        self._load_testsuites()

        for tester in self.testers:
            tester.set_settings(options, args, self.reporter)

        if not options.config and options.testsuites:
            if self._setup_testsuites() is False:
                return False

        for tester in self.testers:
            if not tester.set_blacklists():
                return False

            if not tester.check_expected_failures():
                return False

        return True

    def _check_tester_has_other_testsuite(self, testsuite, tester):
        if tester.name != testsuite.TEST_MANAGER[0]:
            return True

        for t in self.options.testsuites:
            if t != testsuite:
                for other_testmanager in t.TEST_MANAGER:
                    if other_testmanager == tester.name:
                        return True

        return False

    def _check_defined_tests(self, tester, tests):
        if self.options.blacklisted_tests or self.options.wanted_tests:
            return

        tests_names = [test.classname for test in tests]
        testlist_changed = False
        for testsuite in self.options.testsuites:
            if not self._check_tester_has_other_testsuite(testsuite, tester) \
                    and tester.check_testslist:
                try:
                    testlist_file = open(os.path.splitext(testsuite.__file__)[0] + ".testslist",
                                         'r+')

                    know_tests = testlist_file.read().split("\n")
                    testlist_file.close()

                    testlist_file = open(os.path.splitext(testsuite.__file__)[0] + ".testslist",
                                         'w')
                except IOError:
                    return

                for test in know_tests:
                    if test and test not in tests_names:
                        testlist_changed = True
                        printc("Test %s Not in testsuite %s anymore"
                               % (test, testsuite.__file__), Colors.FAIL)

                for test in tests_names:
                    testlist_file.write("%s\n" % test)
                    if test and test not in know_tests:
                        printc("Test %s is NEW in testsuite %s"
                               % (test, testsuite.__file__), Colors.OKGREEN)
                        testlist_changed = True

                testlist_file.close()
                break

        return testlist_changed

    def list_tests(self):
        for tester in self.testers:
            tests = tester.list_tests()
            if self._check_defined_tests(tester, tests) and \
                    self.options.fail_on_testlist_change:
                return -1

            self.tests.extend(tests)
        return sorted(list(self.tests), key=lambda t: t.classname)

    def _tester_needed(self, tester):
        for testsuite in self.options.testsuites:
            if tester.name in testsuite.TEST_MANAGER:
                return True
        return False

    def _run_tests(self):
        cur_test_num = 0

        if not self.all_tests:
            total_num_tests = 1
            self.all_tests = []
            for tester in self.testers:
                if self._tester_needed(tester):
                    self.all_tests.extend(tester.list_tests())
        total_num_tests = len(self.all_tests)

        self.reporter.init_timer()
        for tester in self.testers:
            if not self._tester_needed(tester):
                continue
            res = tester.run_tests(cur_test_num, total_num_tests)
            cur_test_num += len(tester.list_tests())
            if res != Result.PASSED and (self.options.forever or
                                         self.options.fatal_error):
                return False

        return True

    def clean_tests(self):
        for tester in self.testers:
            tester.clean_tests()

    def run_tests(self):
        if self.options.forever:
            r = 1
            while True:
                t = "Running iteration %d" % r
                print("%s\n%s\n%s\n" % ("=" * len(t), t, "=" * len(t)))

                if not self._run_tests():
                    break
                r += 1
                self.clean_tests()

            return False
        elif self.options.n_runs:
            res = True
            for r in range(self.options.n_runs):
                t = "Running iteration %d" % r
                print("%s\n%s\n%s\n" % ("=" * len(t), t, "=" * len(t)))
                if not self._run_tests():
                    res = False
                self.clean_tests()

            return res
        else:
            return self._run_tests()

    def final_report(self):
        self.reporter.final_report()

    def needs_http_server(self):
        for tester in self.testers:
            if tester.needs_http_server():
                return True


class NamedDic(object):

    def __init__(self, props):
        if props:
            for name, value in props.items():
                setattr(self, name, value)


class Scenario(object):

    def __init__(self, name, props, path=None):
        self.name = name
        self.path = path

        for prop, value in props:
            setattr(self, prop.replace("-", "_"), value)

    def get_execution_name(self):
        if self.path is not None:
            return self.path
        else:
            return self.name

    def seeks(self):
        if hasattr(self, "seek"):
            return bool(self.seek)

        return False

    def needs_clock_sync(self):
        if hasattr(self, "need_clock_sync"):
            return bool(self.need_clock_sync)

        return False

    def get_min_media_duration(self):
        if hasattr(self, "min_media_duration"):
            return float(self.min_media_duration)

        return 0

    def does_reverse_playback(self):
        if hasattr(self, "reverse_playback"):
            return bool(self.seek)

        return False

    def get_duration(self):
        try:
            return float(getattr(self, "duration"))
        except AttributeError:
            return 0

    def get_min_tracks(self, track_type):
        try:
            return int(getattr(self, "min_%s_track" % track_type))
        except AttributeError:
            return 0


class ScenarioManager(Loggable):
    _instance = None
    all_scenarios = []

    FILE_EXTENSION = "scenario"
    GST_VALIDATE_COMMAND = ""

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ScenarioManager, cls).__new__(
                cls, *args, **kwargs)
            cls._instance.config = None
            cls._instance.discovered = False
            Loggable.__init__(cls._instance)

        return cls._instance

    def find_special_scenarios(self, mfile):
        scenarios = []
        mfile_bname = os.path.basename(mfile)

        for f in os.listdir(os.path.dirname(mfile)):
            if re.findall("%s\..*\.%s$" % (re.escape(mfile_bname), self.FILE_EXTENSION), f):
                scenarios.append(os.path.join(os.path.dirname(mfile), f))

        if scenarios:
            scenarios = self.discover_scenarios(scenarios, mfile)

        return scenarios

    def discover_scenarios(self, scenario_paths=[], mfile=None):
        """
        Discover scenarios specified in scenario_paths or the default ones
        if nothing specified there
        """
        scenarios = []
        scenario_defs = os.path.join(self.config.main_dir, "scenarios.def")
        logs = open(os.path.join(self.config.logsdir,
                                 "scenarios_discovery.log"), 'w')

        try:
            command = [self.GST_VALIDATE_COMMAND,
                       "--scenarios-defs-output-file", scenario_defs]
            command.extend(scenario_paths)
            subprocess.check_call(command, stdout=logs, stderr=logs)
        except subprocess.CalledProcessError:
            pass

        config = configparser.RawConfigParser()
        f = open(scenario_defs)
        config.readfp(f)

        for section in config.sections():
            if scenario_paths:
                for scenario_path in scenario_paths:
                    if mfile is None:
                        name = section
                        path = scenario_path
                    elif section in scenario_path:
                        # The real name of the scenario is:
                        # filename.REALNAME.scenario
                        name = scenario_path.replace(mfile + ".", "").replace(
                            "." + self.FILE_EXTENSION, "")
                        path = scenario_path
            else:
                name = section
                path = None

            props = config.items(section)
            scenarios.append(Scenario(name, props, path))

        if not scenario_paths:
            self.discovered = True
            self.all_scenarios.extend(scenarios)

        return scenarios

    def get_scenario(self, name):
        if name is not None and os.path.isabs(name) and name.endswith(self.FILE_EXTENSION):
            scenarios = self.discover_scenarios([name])

            if scenarios:
                return scenarios[0]

        if self.discovered is False:
            self.discover_scenarios()

        if name is None:
            return self.all_scenarios

        try:
            return [scenario for scenario in self.all_scenarios if scenario.name == name][0]
        except IndexError:
            self.warning("Scenario: %s not found" % name)
            return None


class GstValidateBaseTestManager(TestsManager):
    scenarios_manager = ScenarioManager()

    def __init__(self):
        super(GstValidateBaseTestManager, self).__init__()
        self._scenarios = []
        self._encoding_formats = []

    def add_scenarios(self, scenarios):
        """
        @scenarios A list or a unic scenario name(s) to be run on the tests.
                    They are just the default scenarios, and then depending on
                    the TestsGenerator to be used you can have more fine grained
                    control on what to be run on each serie of tests.
        """
        if isinstance(scenarios, list):
            self._scenarios.extend(scenarios)
        else:
            self._scenarios.append(scenarios)

        self._scenarios = list(set(self._scenarios))

    def get_scenarios(self):
        return self._scenarios

    def add_encoding_formats(self, encoding_formats):
        """
        :param encoding_formats: A list or one single #MediaFormatCombinations describing wanted output
                           formats for transcoding test.
                           They are just the default encoding formats, and then depending on
                           the TestsGenerator to be used you can have more fine grained
                           control on what to be run on each serie of tests.
        """
        if isinstance(encoding_formats, list):
            self._encoding_formats.extend(encoding_formats)
        else:
            self._encoding_formats.append(encoding_formats)

        self._encoding_formats = list(set(self._encoding_formats))

    def get_encoding_formats(self):
        return self._encoding_formats


class MediaDescriptor(Loggable):

    def __init__(self):
        Loggable.__init__(self)

    def get_path(self):
        raise NotImplemented

    def get_media_filepath(self):
        raise NotImplemented

    def get_caps(self):
        raise NotImplemented

    def get_uri(self):
        raise NotImplemented

    def get_duration(self):
        raise NotImplemented

    def get_protocol(self):
        raise NotImplemented

    def is_seekable(self):
        raise NotImplemented

    def is_image(self):
        raise NotImplemented

    def get_num_tracks(self, track_type):
        raise NotImplemented

    def can_play_reverse(self):
        raise NotImplemented

    def is_compatible(self, scenario):
        if scenario is None:
            return True

        if scenario.seeks() and (not self.is_seekable() or self.is_image()):
            self.debug("Do not run %s as %s does not support seeking",
                       scenario, self.get_uri())
            return False

        if self.is_image() and scenario.needs_clock_sync():
            self.debug("Do not run %s as %s is an image",
                       scenario, self.get_uri())
            return False

        if not self.can_play_reverse() and scenario.does_reverse_playback():
            return False

        if self.get_duration() and self.get_duration() / GST_SECOND < scenario.get_min_media_duration():
            self.debug(
                "Do not run %s as %s is too short (%i < min media duation : %i",
                scenario, self.get_uri(),
                self.get_duration() / GST_SECOND,
                scenario.get_min_media_duration())
            return False

        for track_type in ['audio', 'subtitle', 'video']:
            if self.get_num_tracks(track_type) < scenario.get_min_tracks(track_type):
                self.debug("%s -- %s | At least %s %s track needed  < %s"
                           % (scenario, self.get_uri(), track_type,
                              scenario.get_min_tracks(track_type),
                              self.get_num_tracks(track_type)))
                return False

        return True


class GstValidateMediaDescriptor(MediaDescriptor):
    # Some extension file for discovering results
    MEDIA_INFO_EXT = "media_info"
    STREAM_INFO_EXT = "stream_info"

    DISCOVERER_COMMAND = "gst-validate-media-check-1.0"
    if "win32" in sys.platform:
        DISCOVERER_COMMAND += ".exe"

    def __init__(self, xml_path):
        super(GstValidateMediaDescriptor, self).__init__()

        self._xml_path = xml_path
        self.media_xml = ET.parse(xml_path).getroot()

        # Sanity checks
        self.media_xml.attrib["duration"]
        self.media_xml.attrib["seekable"]

        self.set_protocol(urllib.parse.urlparse(urllib.parse.urlparse(self.get_uri()).scheme).scheme)

    @staticmethod
    def new_from_uri(uri, verbose=False, include_frames=False):
        """
            include_frames = 0 # Never
            include_frames = 1 # always
            include_frames = 2 # if previous file included them

        """
        media_path = utils.url2path(uri)

        descriptor_path = "%s.%s" % (
            media_path, GstValidateMediaDescriptor.MEDIA_INFO_EXT)
        if include_frames == 2:
            try:
                media_xml = ET.parse(descriptor_path).getroot()
                frames = media_xml.findall('streams/stream/frame')
                include_frames = bool(frames)
            except FileNotFoundError:
                pass
        else:
            include_frames = bool(include_frames)

        args = GstValidateMediaDescriptor.DISCOVERER_COMMAND.split(" ")
        args.append(uri)

        args.extend(["--output-file", descriptor_path])
        if include_frames:
            args.extend(["--full"])

        if verbose:
            printc("Generating media info for %s\n"
                   "    Command: '%s'" % (media_path, ' '.join(args)),
                   Colors.OKBLUE)

        try:
            subprocess.check_output(args, stderr=open(os.devnull))
        except subprocess.CalledProcessError as e:
            if verbose:
                printc("Result: Failed", Colors.FAIL)
            else:
                loggable.warning("GstValidateMediaDescriptor", "Exception: %s" % e)
            return None

        if verbose:
            printc("Result: Passed", Colors.OKGREEN)

        try:
            return GstValidateMediaDescriptor(descriptor_path)
        except FileNotFoundError:
            return None

    def get_path(self):
        return self._xml_path

    def need_clock_sync(self):
        return Protocols.needs_clock_sync(self.get_protocol())

    def get_media_filepath(self):
        if self.get_protocol() == Protocols.FILE:
            return self._xml_path.replace("." + self.MEDIA_INFO_EXT, "")
        else:
            return self._xml_path.replace("." + self.STREAM_INFO_EXT, "")

    def get_caps(self):
        return self.media_xml.findall("streams")[0].attrib["caps"]

    def get_tracks_caps(self):
        res = []
        try:
            streams = self.media_xml.findall("streams")[0].findall("stream")
        except IndexError:
            return res

        for stream in streams:
            res.append((stream.attrib["type"], stream.attrib["caps"]))

        return res

    def get_uri(self):
        return self.media_xml.attrib["uri"]

    def get_duration(self):
        return int(self.media_xml.attrib["duration"])

    def set_protocol(self, protocol):
        self.media_xml.attrib["protocol"] = protocol

    def get_protocol(self):
        return self.media_xml.attrib["protocol"]

    def is_seekable(self):
        return self.media_xml.attrib["seekable"].lower() == "true"

    def can_play_reverse(self):
        return True

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

    def get_clean_name(self):
        name = os.path.basename(self.get_path())
        name = re.sub("\.stream_info|\.media_info", "", name)

        return name.replace('.', "_")


class MediaFormatCombination(object):
    FORMATS = {"aac": "audio/mpeg,mpegversion=4",  # Audio
               "ac3": "audio/x-ac3",
               "vorbis": "audio/x-vorbis",
               "mp3": "audio/mpeg,mpegversion=1,layer=3",
               "opus": "audio/x-opus",
               "rawaudio": "audio/x-raw",

               # Video
               "h264": "video/x-h264",
               "h265": "video/x-h265",
               "vp8": "video/x-vp8",
               "vp9": "video/x-vp9",
               "theora": "video/x-theora",
               "prores": "video/x-prores",
               "jpeg": "image/jpeg",

               # Containers
               "webm": "video/webm",
               "ogg": "application/ogg",
               "mkv": "video/x-matroska",
               "mp4": "video/quicktime,variant=iso;",
               "quicktime": "video/quicktime;"}

    def __str__(self):
        return "%s and %s in %s" % (self.audio, self.video, self.container)

    def __init__(self, container, audio, video):
        """
        Describes a media format to be used for transcoding tests.

        :param container: A string defining the container format to be used, must bin in self.FORMATS
        :param audio: A string defining the audio format to be used, must bin in self.FORMATS
        :param video: A string defining the video format to be used, must bin in self.FORMATS
        """
        self.container = container
        self.audio = audio
        self.video = video

    def get_caps(self, track_type):
        try:
            return self.FORMATS[self.__dict__[track_type]]
        except KeyError:
            return None

    def get_audio_caps(self):
        return self.get_caps("audio")

    def get_video_caps(self):
        return self.get_caps("video")

    def get_muxer_caps(self):
        return self.get_caps("container")
