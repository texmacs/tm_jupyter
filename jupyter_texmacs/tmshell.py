"""IPython terminal interface using TeXmacs protocol"""

from __future__ import print_function

import base64
import errno
from getpass import getpass
from io import BytesIO
import os
import signal
import subprocess
import sys
import time
from warnings import warn

try:
    from queue import Empty  # Py 3
except ImportError:
    from Queue import Empty  # Py 2

from zmq import ZMQError
from IPython.core import page
from IPython.utils.py3compat import cast_unicode_py2, input
from ipython_genutils.tempdir import NamedFileInTemporaryDirectory
from traitlets import (Bool, Integer, Float, Unicode, List, Dict, Enum,
                       Instance, Any)
from traitlets.config import SingletonConfigurable

from .completer import ZMQCompleter
from .zmqhistory import ZMQHistoryManager
from . import __version__

from .protocol import *


py_ver = sys.version_info[0]
if py_ver == 3:
    _input = input
else:
    _input = raw_input


def ask_yes_no(prompt, default=None, interrupt=None):
    """Asks a question and returns a boolean (y/n) answer.

    If default is given (one of 'y','n'), it is used if the user input is
    empty. If interrupt is given (one of 'y','n'), it is used if the user
    presses Ctrl-C. Otherwise the question is repeated until an answer is
    given.

    An EOF is treated as the default answer.  If there is no default, an
    exception is raised to prevent infinite loops.

    Valid answers are: y/yes/n/no (match is not case sensitive)."""

    answers = {'y': True, 'n': False, 'yes': True, 'no': False}
    ans = None
    while ans not in answers.keys():
        try:
            ans = input(prompt + ' ').lower()
            if not ans:  # response was an empty string
                ans = default
        except KeyboardInterrupt:
            if interrupt:
                ans = interrupt
        except EOFError:
            if default in answers.keys():
                ans = default
                print()
            else:
                raise

    return answers[ans]


class ZMQTerminalInteractiveShell(SingletonConfigurable):
    readline_use = False

    pt_cli = None

    _executing = False
    _execution_state = Unicode('')
    _pending_clearoutput = False
    _eventloop = None
    own_kernel = False  # Changed by ZMQTerminalIPythonApp

    history_load_length = Integer(1000, config=True,
        help="How many history items to load into memory"
    )

    banner = Unicode('Jupyter console {version}\n\n{kernel_banner}', config=True,
        help=("Text to display before the first prompt. Will be formatted with "
              "variables {version} and {kernel_banner}.")
    )

    kernel_timeout = Float(60, config=True,
        help="""Timeout for giving up on a kernel (in seconds).

        On first connect and restart, the console tests whether the
        kernel is running and responsive by sending kernel_info_requests.
        This sets the timeout in seconds for how long the kernel can take
        before being presumed dead.
        """
    )

    image_handler = Enum(('PIL', 'stream', 'tempfile', 'callable'),
                         'PIL', config=True, allow_none=True, help=
        """
        Handler for image type output.  This is useful, for example,
        when connecting to the kernel in which pylab inline backend is
        activated.  There are four handlers defined.  'PIL': Use
        Python Imaging Library to popup image; 'stream': Use an
        external program to show the image.  Image will be fed into
        the STDIN of the program.  You will need to configure
        `stream_image_handler`; 'tempfile': Use an external program to
        show the image.  Image will be saved in a temporally file and
        the program is called with the temporally file.  You will need
        to configure `tempfile_image_handler`; 'callable': You can set
        any Python callable which is called with the image data.  You
        will need to configure `callable_image_handler`.
        """
    )

    stream_image_handler = List(config=True, help=
        """
        Command to invoke an image viewer program when you are using
        'stream' image handler.  This option is a list of string where
        the first element is the command itself and reminders are the
        options for the command.  Raw image data is given as STDIN to
        the program.
        """
    )

    tempfile_image_handler = List(config=True, help=
        """
        Command to invoke an image viewer program when you are using
        'tempfile' image handler.  This option is a list of string
        where the first element is the command itself and reminders
        are the options for the command.  You can use {file} and
        {format} in the string to represent the location of the
        generated image file and image format.
        """
    )

    callable_image_handler = Any(config=True, help=
        """
        Callable object called via 'callable' image handler with one
        argument, `data`, which is `msg["content"]["data"]` where
        `msg` is the message from iopub channel.  For example, you can
        find base64 encoded PNG data as `data['image/png']`. If your function
        can't handle the data supplied, it should return `False` to indicate
        this.
        """
    )

    mime_preference = List(
        default_value=['image/png', 'image/jpeg', 'image/svg+xml'],
        config=True, help=
        """
        Preferred object representation MIME type in order.  First
        matched MIME type will be used.
        """
    )

    use_kernel_is_complete = Bool(True, config=True,
        help="""Whether to use the kernel's is_complete message
        handling. If False, then the frontend will use its
        own is_complete handler.
        """
    )
    kernel_is_complete_timeout = Float(1, config=True,
        help="""Timeout (in seconds) for giving up on a kernel's is_complete
        response.

        If the kernel does not respond at any point within this time,
        the kernel will no longer be asked if code is complete, and the
        console will default to the built-in is_complete test.
        """
    )

    # This is configurable on JupyterConsoleApp; this copy is not configurable
    # to avoid a duplicate config option.
    confirm_exit = Bool(True,
        help="""Set to display confirmation dialog on exit.
        You can always use 'exit' or 'quit', to force a
        direct exit without any confirmation.
        """
    )

    manager = Instance('jupyter_client.KernelManager', allow_none=True)
    client = Instance('jupyter_client.KernelClient', allow_none=True)

    def _client_changed(self, name, old, new):
        self.session_id = new.session.session
    session_id = Unicode()

    def _banner1_default(self):
        return "Jupyter Console {version}\n".format(version=__version__)

    simple_prompt = Bool(False,
         help="""Use simple fallback prompt. Features may be limited."""
    ).tag(config=True)

    def __init__(self, **kwargs):
        # This is where traits with a config_key argument are updated
        # from the values on config.
        super(ZMQTerminalInteractiveShell, self).__init__(**kwargs)
        self.configurables = [self]

        self.init_history()
        self.init_completer()

        self.init_kernel_info()
        self.init_prompt_toolkit_cli()
        self.keep_running = True
        self.execution_count = 1

    def init_completer(self):
        """Initialize the completion machinery.

        This creates completion machinery that can be used by client code,
        either interactively in-process (typically triggered by the readline
        library), programmatically (such as in test suites) or out-of-process
        (typically over the network by remote frontends).
        """
        self.Completer = ZMQCompleter(self, self.client, config=self.config)

    def init_history(self):
        """Sets up the command history. """
        self.history_manager = ZMQHistoryManager(client=self.client)
        self.configurables.append(self.history_manager)

    def get_prompt_tokens(self):
        return [
            (Token.Prompt, 'In ['),
            (Token.PromptNum, str(self.execution_count)),
            (Token.Prompt, ']: '),
        ]

    def get_continuation_tokens(self, width):
        return [
            (Token.Prompt, (' ' * (width - 2)) + ': '),
        ]

    def get_out_prompt_tokens(self):
        return [
            (Token.OutPrompt, 'Out['),
            (Token.OutPromptNum, str(self.execution_count)),
            (Token.OutPrompt, ']: ')
        ]

    def print_out_prompt(self):
        tokens = self.get_out_prompt_tokens()
        print_formatted_text(PygmentsTokens(tokens), end='',
                             style = self.pt_cli.app.style)

    kernel_info = {}

    def init_kernel_info(self):
        """Wait for a kernel to be ready, and store kernel info"""
        timeout = self.kernel_timeout
        tic = time.time()
        self.client.hb_channel.unpause()
        msg_id = self.client.kernel_info()
        while True:
            try:
                reply = self.client.get_shell_msg(timeout=1)
            except Empty:
                if (time.time() - tic) > timeout:
                    raise RuntimeError("Kernel didn't respond to kernel_info_request")
            else:
                if reply['parent_header'].get('msg_id') == msg_id:
                    self.kernel_info = reply['content']
                    return

    def show_banner(self):
        flush_verbatim (self.banner.format(version=__version__,
                         kernel_banner=self.kernel_info.get('banner', '')))

    def print_out_prompt(self):
        flush_verbatim ('Out[%d]: ' % self.execution_count)

    def init_prompt_toolkit_cli(self):
        # Pre-populate history from IPython's history database
        history = []
        last_cell = u""
        for _, _, cell in self.history_manager.get_tail(self.history_load_length,
                                                        include_latest=True):
            # Ignore blank lines and consecutive duplicates
            cell = cast_unicode_py2(cell.rstrip())
            if cell and (cell != last_cell):
                history.append(cell)

    def check_complete(self, code):
        if self.use_kernel_is_complete:
            msg_id = self.client.is_complete(code)
            try:
                return self.handle_is_complete_reply(msg_id,
                                                     timeout=self.kernel_is_complete_timeout)
            except SyntaxError:
                return False, ""
        else:
            lines = code.splitlines()
            if len(lines):
                more = (lines[-1] != "")
                return more, ""
            else:
                return False, ""

    def ask_exit(self):
        self.keep_running = False

    # This is set from payloads in handle_execute_reply
    next_input = None

    def mainloop(self):
        self.keepkernel = not self.own_kernel

        # Main session loop.
        while self.keep_running:
            flush_prompt ('In [%d]: ' % self.execution_count)
            line = _input ()
            if not line:
                continue
            lines = [line]
            while line != "<EOF>":
                line = _input ()
                if line == '': 
                    continue
                lines.append(line)
            code = '\n'.join(lines[:-1])
            self.run_cell(code, store_history=True)

        if self._eventloop:
            self._eventloop.close()
        if self.keepkernel and not self.own_kernel:
            flush_verbatim ('keeping kernel alive')
        elif self.keepkernel and self.own_kernel:
            flush_verbatim ("owning kernel, cannot keep it alive")
            self.client.shutdown()
        else:
            flush_verbatim ("Shutting down kernel")
            self.client.shutdown()

    def run_cell(self, cell, store_history=True):
        """Run a complete IPython cell.

        Parameters
        ----------
        cell : str
          The code (including IPython code such as %magic functions) to run.
        store_history : bool
          If True, the raw and translated cell will be stored in IPython's
          history. For user code calling back into IPython's machinery, this
          should be set to False.
        """
        if (not cell) or cell.isspace():
            # pressing enter flushes any pending display
            self.handle_iopub()
            return

        # flush stale replies, which could have been ignored, due to missed heartbeats
        while self.client.shell_channel.msg_ready():
            self.client.shell_channel.get_msg()
        # execute takes 'hidden', which is the inverse of store_hist
        msg_id = self.client.execute(cell, not store_history)

        # first thing is wait for any side effects (output, stdin, etc.)
        self._executing = True
        self._execution_state = "busy"
        while self._execution_state != 'idle' and self.client.is_alive():
            try:
                self.handle_input_request(msg_id, timeout=0.05)
            except Empty:
                # display intermediate print statements, etc.
                self.handle_iopub(msg_id)
            except ZMQError as e:
                # Carry on if polling was interrupted by a signal
                if e.errno != errno.EINTR:
                    raise

        # after all of that is done, wait for the execute reply
        while self.client.is_alive():
            try:
                self.handle_execute_reply(msg_id, timeout=0.05)
            except Empty:
                pass
            else:
                break
        self._executing = False

    #-----------------
    # message handlers
    #-----------------

    def handle_execute_reply(self, msg_id, timeout=None):
        msg = self.client.shell_channel.get_msg(block=False, timeout=timeout)
        if msg["parent_header"].get("msg_id", None) == msg_id:

            self.handle_iopub(msg_id)

            content = msg["content"]
            status = content['status']

            if status == 'aborted':
                self.write('Aborted\n') ##FIXME: What to do with this?
                return
            elif status == 'ok':
                # handle payloads
                for item in content.get("payload", []):
                    source = item['source']
                    if source == 'page':
                        page.page(item['data']['text/plain'])
                    elif source == 'set_next_input':
                        self.next_input = item['text']
                    elif source == 'ask_exit':
                        self.keepkernel = item.get('keepkernel', False)
                        self.ask_exit()

            elif status == 'error':
                pass

            self.execution_count = int(content["execution_count"] + 1)

    def handle_is_complete_reply(self, msg_id, timeout=None):
        """
        Wait for a repsonse from the kernel, and return two values:
            more? - (boolean) should the frontend ask for more input
            indent - an indent string to prefix the input
        Overloaded methods may want to examine the comeplete source. Its is
        in the self._source_lines_buffered list.
        """
        ## Get the is_complete response:
        msg = None
        try:
            msg = self.client.shell_channel.get_msg(block=True, timeout=timeout)
        except Empty:
            warn('The kernel did not respond to an is_complete_request. '
                 'Setting `use_kernel_is_complete` to False.')
            self.use_kernel_is_complete = False
            return False, ""
        ## Handle response:
        if msg["parent_header"].get("msg_id", None) != msg_id:
            warn('The kernel did not respond properly to an is_complete_request: %s.' % str(msg))
            return False, ""
        else:
            status = msg["content"].get("status", None)
            indent = msg["content"].get("indent", "")
        ## Return more? and indent string
        if status == "complete":
            return False, indent
        elif status == "incomplete":
            return True, indent
        elif status == "invalid":
            raise SyntaxError()
        elif status == "unknown":
            return False, indent
        else:
            warn('The kernel sent an invalid is_complete_reply status: "%s".' % status)
            return False, indent

    include_other_output = Bool(False, config=True,
        help="""Whether to include output from clients
        other than this one sharing the same kernel.

        Outputs are not displayed until enter is pressed.
        """
    )
    other_output_prefix = Unicode("[remote] ", config=True,
        help="""Prefix to add to outputs coming from clients other than this one.

        Only relevant if include_other_output is True.
        """
    )

    def from_here(self, msg):
        """Return whether a message is from this session"""
        return msg['parent_header'].get("session", self.session_id) == self.session_id

    def include_output(self, msg):
        """Return whether we should include a given output message"""
        from_here = self.from_here(msg)
        if msg['msg_type'] == 'execute_input':
            # only echo inputs not from here
            return self.include_other_output and not from_here

        if self.include_other_output:
            return True
        else:
            return from_here

    def handle_iopub(self, msg_id=''):
        """Process messages on the IOPub channel

           This method consumes and processes messages on the IOPub channel,
           such as stdout, stderr, execute_result and status.

           It only displays output that is caused by this session.
        """
        while self.client.iopub_channel.msg_ready():
            sub_msg = self.client.iopub_channel.get_msg()
            msg_type = sub_msg['header']['msg_type']
            parent = sub_msg["parent_header"]

            # Update execution_count in case it changed in another session
            if msg_type == "execute_input":
                self.execution_count = int(sub_msg["content"]["execution_count"]) + 1

            if self.include_output(sub_msg):
                if msg_type == 'status':
                    self._execution_state = sub_msg["content"]["execution_state"]
                elif msg_type == 'stream':
                    if sub_msg["content"]["name"] == "stdout":
                        if self._pending_clearoutput:
                            flush_verbatim ("\r")
                            self._pending_clearoutput = False
                        flush_verbatim (sub_msg["content"]["text"])
                        sys.stdout.flush()
                    elif sub_msg["content"]["name"] == "stderr":
                        if self._pending_clearoutput:
                            flush_err ("\r")
                            self._pending_clearoutput = False
                        flush_err (sub_msg["content"]["text"])
                        sys.stderr.flush()
                elif msg_type == 'execute_result':
                    if self._pending_clearoutput:
                        flush_verbatim ("\r")
                        self._pending_clearoutput = False
                    self.execution_count = int(sub_msg["content"]["execution_count"])
                    if not self.from_here(sub_msg):
                        flush_verbatim (self.other_output_prefix)
                    format_dict = sub_msg["content"]["data"]
                    self.handle_rich_data(format_dict)

                    if 'text/plain' not in format_dict:
                        continue

                    # prompt_toolkit writes the prompt at a slightly lower level,
                    # so flush streams first to ensure correct ordering.
                    sys.stdout.flush()
                    sys.stderr.flush()
                    self.print_out_prompt()
                    text_repr = format_dict['text/plain']
                    if '\n' in text_repr:
                        # For multi-line results, start a new line after prompt
                        flush_verbatim ('\n')
                    flush_verbatim (text_repr)

                elif msg_type == 'display_data':
                    data = sub_msg["content"]["data"]
                    handled = self.handle_rich_data(data)
                    if not handled:
                        if not self.from_here(sub_msg):
                            sys.stdout.write(self.other_output_prefix)
                        # if it was an image, we handled it by now
                        if 'text/plain' in data:
                            flush_verbatim (data['text/plain'])

                elif msg_type == 'execute_input':
                    content = sub_msg['content']
                    if not self.from_here(sub_msg):
                        flush_verbatim (self.other_output_prefix)
                    flush_verbatim ('In [{}]: '.format(content['execution_count']))
                    flush_verbatim (content['code'] + '\n')

                elif msg_type == 'clear_output':
                    if sub_msg["content"]["wait"]:
                        self._pending_clearoutput = True
                    else:
                        flush_verbatim ("\r")

                elif msg_type == 'error':
                    for frame in sub_msg["content"]["traceback"]:
                        flush_err (frame)

    _imagemime = {
        'image/png': 'png',
        'image/jpeg': 'jpeg',
        'image/svg+xml': 'svg',
    }

    def handle_rich_data(self, data):
        for mime in self.mime_preference:
            if mime in data and mime in self._imagemime:
                if self.handle_image(data, mime):
                    return True
        return False

    def handle_image(self, data, mime):
        handler = getattr(
            self, 'handle_image_{0}'.format(self.image_handler), None)
        if handler:
            return handler(data, mime)

    def handle_image_PIL(self, data, mime):
        if mime not in ('image/png', 'image/jpeg'):
            return False
        try:
            from PIL import Image, ImageShow
        except ImportError:
            return False
        raw = base64.decodestring(data[mime].encode('ascii'))
        img = Image.open(BytesIO(raw))
        return ImageShow.show(img)

    def handle_image_stream(self, data, mime):
        raw = base64.decodestring(data[mime].encode('ascii'))
        imageformat = self._imagemime[mime]
        fmt = dict(format=imageformat)
        args = [s.format(**fmt) for s in self.stream_image_handler]
        with open(os.devnull, 'w') as devnull:
            proc = subprocess.Popen(
                args, stdin=subprocess.PIPE,
                stdout=devnull, stderr=devnull)
            proc.communicate(raw)
        return (proc.returncode == 0)

    def handle_image_tempfile(self, data, mime):
        raw = base64.decodestring(data[mime].encode('ascii'))
        imageformat = self._imagemime[mime]
        filename = 'tmp.{0}'.format(imageformat)
        with NamedFileInTemporaryDirectory(filename) as f, \
                open(os.devnull, 'w') as devnull:
            f.write(raw)
            f.flush()
            fmt = dict(file=f.name, format=imageformat)
            args = [s.format(**fmt) for s in self.tempfile_image_handler]
            rc = subprocess.call(args, stdout=devnull, stderr=devnull)
        return (rc == 0)

    def handle_image_callable(self, data, mime):
        res = self.callable_image_handler(data)
        if res is not False:
            # If handler func returns e.g. None, assume it has handled the data.
            res = True
        return res

    def handle_input_request(self, msg_id, timeout=0.1):
        """ Method to capture raw_input
        """
        req = self.client.stdin_channel.get_msg(timeout=timeout)
        # in case any iopub came while we were waiting:
        self.handle_iopub(msg_id)
        if msg_id == req["parent_header"].get("msg_id"):
            # wrap SIGINT handler
            real_handler = signal.getsignal(signal.SIGINT)

            def double_int(sig, frame):
                # call real handler (forwards sigint to kernel),
                # then raise local interrupt, stopping local raw_input
                real_handler(sig, frame)
                raise KeyboardInterrupt
            signal.signal(signal.SIGINT, double_int)
            content = req['content']
            read = getpass if content.get('password', False) else input
            try:
                raw_data = read(content["prompt"])
            except EOFError:
                # turn EOFError into EOF character
                raw_data = '\x04'
            except KeyboardInterrupt:
                sys.stdout.write('\n')
                return
            finally:
                # restore SIGINT handler
                signal.signal(signal.SIGINT, real_handler)

            # only send stdin reply if there *was not* another request
            # or execution finished while we were reading.
            if not (self.client.stdin_channel.msg_ready() or
                    self.client.shell_channel.msg_ready()):
                self.client.input(raw_data)