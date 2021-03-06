# vim: tabstop=3:shiftwidth=3:expandtab:autoindent

# 'Telnet' multiplexer for MUDs, etc. Python 3.

# Things we should be doing someday:
#  - actually understand the underlying protocols (Telnet) instead of just stripping
#    them out

import sys
import threading
import logging
import traceback

import socket
import ssl
import selectors

import json

import os
import hashlib
import getpass
import base64

import pkgutil
import importlib

import ansi


CONFIG_FILE = 'config.json'
PASSWORD_FILE = 'password.json' # Password hash is stored here


ENCODING = 'utf-8'  # (default)
LINE_SEPARATOR = 10 # (default) ASCII/UTF-8 newline

COMMAND_PREFIX = ','
MESSAGE_PREFIX_OK = '%% '
MESSAGE_PREFIX_ERR = '!! '

RECV_MAX = 4096 # bytes

# (defaults; change in config.json)
BIND_TO_HOST = "localhost"
BIND_TO_PORT = 1234

# We want cfg to be global, but not to load it on module import.
cfg = None


logging.basicConfig(level=logging.INFO)


###
### UTILITY FUNCTIONS
###


def load_json(filename):
   try:
      with open(filename, 'r') as f:
         cfg = json.load(f)
      return cfg

   except FileNotFoundError:
      logging.info("Could not read JSON file {}".format(filename))
      return None # ... we could just ignore it and let caller handle the
                  # exception, but in this case since we're trying to *get
                  # a value* it seems more appropriate to return no value
                  # if there's nothing to get.


def save_json(data, filename):
   try:
      with open(filename, 'w') as f:
         json.dump(data, f, indent=3)

   except Exception as e:
      logging.error("Could not write JSON to file {}".format(filename))
      raise e


###
### PASSWORD
###

# We want to make sure random people can't sneak in and access the proxy, even
# if it's on a local-area network.  We use a single password for this.

# After reading some things, I chose to use...scrypt.  I am not a cryptographer,
# but it looked reasonable, it was in the Python standard library, and it seemed
# possible it was more secure than sha256.  It's unlikely this particular application
# will be attacked by a serious password-cracker anyway, but you never really
# know...

class Password:
   """Stores a hashed user password using scrypt.

   When it is initialized, it will try to load the hash from a file; if it can't manage
   to do that, it will block on initialization to prompt the user for a new password
   (and try to persist that to the file.)"""
   def __init__(self):
      self.hashed = load_json(PASSWORD_FILE)
      if self.hashed is None:
         self.prompt_user_for_new_password()
         print(repr(self.hashed))
         save_json({k: base64.b64encode(v).decode('ascii') for k, v in self.hashed.items()}, PASSWORD_FILE)
      else:
         self.hashed = {k: base64.b64decode(v) for k, v in self.hashed.items()}

   def hash(self, password, salt):
      """This function should return a bytes-like object containing the hash of 'password'
      given the salt 'salt'.  The password argument should be a string."""
      hashtype = cfg.get('password_hash_method', 'scrypt')

      if hashtype == 'scrypt':
         # https://blog.filippo.io/the-scrypt-parameters/ was used for a reference for
         # what these mean and what to set them to.
         return hashlib.scrypt(password.encode('utf8'), salt=salt, n=2 ** 15, r=8, p=1, maxmem=1024 * 1024 * 64)
      elif hashtype == 'pbkdf2':
         return hashlib.pbkdf2_hmac('sha256', password.encode('utf8'), salt, 1000000)
      else:
         raise ValueError("Invalid password-hashing method '{}'".format(hashtype))

   def prompt_user_for_new_password(self):
      """Prompt the user for a new password on the console and setup self to verify it later."""
      salt = os.urandom(16)

      pw = None
      while pw is None:
         pw_one = getpass.getpass(prompt='No password to access the proxy has been set.\nEnter one now: ')
         pw_two = getpass.getpass(prompt='Confirm password: ')

         if pw_two == pw_one:
            pw = pw_one

      self.hashed = {'salt': salt, 'hash': self.hash(pw, salt)}

   def verify(self, candidate_password):
      """Check `candidate_password' (a string) against self; return whether it's right."""
      if self.hashed is None:
         raise ValueError("Tried to check a password that doesn't exist.")

      candidate = self.hash(candidate_password, self.hashed['salt'])
      if candidate == self.hashed['hash']:
         return True
      else:
         return False


###
### NETWORKING
###


class TextLine:
   """An abstract container for lines of text.  This seemed like an important
   design element at one point, but it may not be nearly as important now."""
   def __init__(self, string, encoding):
      assert type(string) == bytes or type(string) == str
      self.__enc = encoding

      self.set(string)

   def set(self, string):
      """(Temporary method for testing.)"""
      if type(string) == bytes:
         self.__raw = string
      else:
         self.__raw = string.encode(self.__enc)

   def as_str(self):
      """Try to 'safely', but lossily, decode the raw line into an ordinary string,
      according to the encoding given."""
      s = ""
      r = self.__raw

      while len(r) > 0:
         try:
            s += r.decode(self.__enc)
            r = ''
         except UnicodeDecodeError as e:
            s += r[:e.start].decode(self.__enc)

            for byte in r[e.start:e.end]:
               s += '?(' + str(byte) + ')'

            r = r[e.end:]

      return s

   def as_bytes(self):
      return self.__raw


class LineBufferingSocketContainer:
   """A base class that helps handle reading from and writing to a socket.  The
   I/O is buffered to lines, and telnet control codes (i.e. IAC ...) are dropped.
   (Thus this class only works with servers that are willing to play dumb.  But
   that's most servers, luckily for us.)"""
   def __init__(self, socket = None):
      self.__b_send_buffer = b''
      self.__b_recv_buffer = b''

      self.connected = False

      self.socket = None

      self.encoding = ENCODING
      self.linesep = LINE_SEPARATOR

      if socket != None:
         self.attach_socket(socket)

   def write_str(self, data):
      """Write a string to the underlying socket."""
      assert type(data) == str

      self.__b_send_buffer += data.encode(self.encoding)

      self.flush()

   def write_line(self, line):
      """Write a TextLine to the underlying socket."""
      assert type(line) == TextLine

      self.__b_send_buffer += line.as_bytes()

      self.flush()

   def write(self, data):
      """Write some bytes to the underlying socket."""
      assert type(data) == bytes

      self.__b_send_buffer += data

      self.flush()

   def flush(self):
      """Send as much buffered input as the socket will allow, but only attempt to
      do so up to the end of the last complete line."""
      assert self.socket != None
      assert self.connected

      while len(self.__b_send_buffer) > 0 and self.linesep in self.__b_send_buffer:
         try:
            t = self.__b_send_buffer.index(self.linesep)
            n_bytes = self.socket.send(self.__b_send_buffer[:t+1])
            self.__b_send_buffer = self.__b_send_buffer[n_bytes:]

         except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
            logging.info("Note: BlockingIOError in flush() call")
            break

         except OSError:
            logging.error("Got an OSError in flush() call")
            break

   def read(self):
      """Read as much data as the socket will provide.  Returns a pair like `([list of TextLine's or empty],
      found_eof?)'.  If found_eof? is true, the connection has probably died."""
      assert self.connected
      assert self.socket != None

      has_eof = False

      try:
         data = b''
         while True:
            data = self.socket.recv(RECV_MAX)
            self.__b_recv_buffer += data
            if len(data) < RECV_MAX:
               # If the length of data returned by a read() call is 0, that actually means the
               # remote side closed the connection.  If there's actually no data to be read,
               # you get a BlockingIOError or one of its SSL-based cousins instead.
               if len(data) == 0:
                  has_eof = True
               break
            data = b''

      except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
         pass

      except OSError:
         logging.error("Got an OSError in read() call")

      except ConnectionResetError:
         has_eof = True

      q = []

      # Telnet codes are a problem.  TODO: Improve this super hacky solution, which just involves
      # ... completely removing them from the input stream (except for IAC IAC / 255 255.)

      stripped = b''

      IAC = 255
      DONT = 254
      DO = 253
      WONT = 252
      WILL = 251

      in_command = False

      # Speaking of awful hacks, this is probably not very efficient at all:

      x = 0
      while x < len(self.__b_recv_buffer):
         if in_command:
            if self.__b_recv_buffer[x] == IAC:
               stripped += bytes([IAC])
               in_command = False
            elif self.__b_recv_buffer[x] <= DONT and self.__b_recv_buffer[x] >= WILL:
               pass
            else:
               # TODO: Figure out if there are Telnet codes that will be baffled by this
               # (are they all guaranteed to be 2 bytes long except for IAC <DODONTWILLWONT> XYZ?)
               in_command = False
         else:
            if self.__b_recv_buffer[x] == IAC:
               in_command = True
            else:
               stripped += self.__b_recv_buffer[x:x+1]
         x += 1

      # The best we can do for a record separator in this case is a byte or byte sequence that
      # means 'newline'. We go with one byte for now for simplicity & because it works with
      # UTF-8/ASCII at least, which comprises most things we're interested in.

      while self.linesep in stripped:
         t = stripped.index(self.linesep)
         q += [TextLine(stripped[:t+1], self.encoding)]
         stripped = stripped[t+1:]

      self.__b_recv_buffer = stripped

      # Make sure it starts in in_command mode again next time around in case the read() call
      # left us in the middle of a command, which I don't think is *likely* but could happen.
      # (The rest of the command will get tacked on after the IAC, which will ensure
      # the thing goes back into command mode immediately prior.)

      if in_command:
         self.__b_send_buffer += bytes([IAC])

      return (q, has_eof)

   def attach_socket(self, socket):
      """Set up `self' to work with `socket'."""
      socket.setblocking(False)
      self.socket = socket
      self.connected = True

   def handle_disconnect(self):
      """Call this function when the remote end closed the connection to nullify and
      make false the appropriate variables."""
      self.socket = None
      self.connected = False


class FilterSpecificationError(Exception):
   """This Exception subclass is thrown when the user has made an errror specifying a
   filter or paramters for setting up said filter."""
   pass


class FilteredSocket(LineBufferingSocketContainer):
   """This class mostly extends LineBufferingSocketContainer with a list of filters and
   logic for setting it up from a specification."""
   # Doesn't actually filter *itself* (yet?)
   # Would probably need to override some methods of the parent class.
   # (It may be impracticable to self-filter here anyway because the filters need to
   # know whether their text came from a server or a client and this class is too
   # abstract to know that. But this seemed like the best way to avoid code duplication.)
   def __init__(self):
      super().__init__()
      self.filters = []

   def add_filters(self, filters, prototypes):
      """Add filters to self according to the specification in `filters` (same format as
      configuration file), drawing from the filter prototypes/classes in the dictinoary
      `prototypes`.  Can raise FilterSpecificationError."""

      if type(filters) != list:
         raise FilterSpecificationError("Filters must be specified as list of [name,opts] pairs")

      for f in filters:
         if type(f) != list or len(f) != 2 or type(f[0]) != str or type(f[1]) != dict:
            raise FilterSpecificationError("Format to specify a filter is ['filtername',{'option':'val',...}]")

         filter_name = f[0]
         filter_opts = f[1]

         if filter_name not in prototypes:
            raise FilterSpecificationError("No such filter `{}'".format(filter_name))
            return

         filter_class = prototypes[filter_name]

         self.filters.append(filter_class(self, filter_opts))


class RemoteServer(FilteredSocket):
   """Handles a connection to a remote server, something multiple clients can connect to."""
   def __init__(self, host, port, name=""):
      super().__init__()

      assert type(host) is str
      assert type(name) is str
      assert type(port) is int

      self.host = host
      self.port = port
      self.name = name

      self.subscribers = []

      self.connecting_in_thread = False
      self.use_SSL = False

   def handle_data(self, data):
      """Called when some data has arrived and needs to be dispatched to the subscribers."""
      for sub in self.subscribers:
         sub.write_line(data)

   def attach_socket(self, socket):
      """Set up to use socket `socket'.  Overridden to notify any filters when a server is connected."""
      super().attach_socket(socket)

      for f in self.filters:
         try:
            f.server_connect(True)
         except AttributeError:
            pass

   def handle_disconnect(self):
      """Called when the connection has been lost."""
      super().handle_disconnect()
      for sub in self.subscribers:
         sub.tell_err("Remote server closed connection.")

      for f in self.filters:
         try:
            f.server_connect(False)
         except AttributeError:
            pass

   def subscribe(self, supplicant):
      """Add `supplicant' to the list of subscribed clients."""
      assert type(supplicant) == LocalClient
      if supplicant not in self.subscribers:
         self.subscribers.append(supplicant)

   def unsubscribe(self, supplicant):
      """Remove `supplicant' from the list of subscribed clients."""
      assert type(supplicant) == LocalClient
      while supplicant in self.subscribers:
         self.subscribers.remove(supplicant)

   def tell_all(self, msg):
      """Tell all the clients subscribed to this particular server of something."""
      assert type(msg) == str
      for sub in self.subscribers:
         sub.tell_ok(msg)

   def warn_all(self, msg):
      """Warn all the clients subscribed to this particular server about something."""
      assert type(msg) == str
      for sub in self.subscribers:
         sub.tell_err(msg)


class LocalClient(FilteredSocket):
   def __init__(self, socket):
      super().__init__()

      self.attach_socket(socket)
      self.subscribedTo = None

   def tell_ok(self, msg):
      self.write_str(MESSAGE_PREFIX_OK + msg + "\r\n")

   def tell_err(self, msg):
      self.write_str(MESSAGE_PREFIX_ERR + msg + "\r\n")

   def unsubscribe(self):
      if type(self.subscribedTo) == RemoteServer:
         self.subscribedTo.unsubscribe(self)
         self.subscribedTo = None
      else:
         raise ValueError("client.unsubscribe when subscribedTo not a RemoteServer")

   def subscribe(self, other):
      assert type(other) == RemoteServer
      self.subscribedTo = other

   def handle_data(self, data):
      if self.subscribedTo == None:
         self.tell_err("Not subscribedTo anything.")
         return

      if self.subscribedTo.connected:
         self.subscribedTo.write_line(data)
      else:
         self.tell_err("Remote server not connected.")

   def attach_socket(self, socket):
      # Overridden to notify things when a client is connected.
      super().attach_socket(socket)

      for f in self.filters:
         try:
            f.server_connect(True)
         except AttributeError:
            pass

   def handle_disconnect(self):
      super().handle_disconnect()

      if self.subscribedTo != None:
         self.subscribedTo.unsubscribe(self)

      for f in self.filters:
         try:
            f.client_connect(False)
         except AttributeError:
            pass


###
### PROXY
###


class Proxy:
   def __init__(self, cfg):
      self.LOCK = threading.Lock()
      self.sel = selectors.DefaultSelector()
      self.socket_wrappers = {}

      self.tls_ctx_remote = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
      self.tls_ctx_local  = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
      self.tls_ctx_remote.check_hostname = False
      # This is insecure and bad.  IDEALLY everything would serve a correctly set up
      # certificate and it would Just Work.  But ...
      self.tls_ctx_remote.verify_mode = ssl.CERT_NONE
      self.tls_ctx_local.load_cert_chain("ssl/cert.pem")

      self.servers = {}             # index of available servers by display name
      self.server_sockets = []

      self.client_sockets = []
      self.client_commands = {}

      self.unauthenticated_sockets = []
      self.password = Password()

      self.states = [(self.server_sockets, self.handle_line_server),
                     (self.unauthenticated_sockets, self.handle_line_auth),
                     (self.client_sockets, self.handle_line_client)]

      if cfg.get('debug', False):
         self.register_command("e", self.do_client_debug)
         self.register_command("eval", self.do_client_debug)
         self.register_command("debug", self.do_client_debug)
      self.register_command("J", self.do_client_connect)
      self.register_command("connect", self.do_client_connect)
      self.register_command("j", self.do_client_join)
      self.register_command("join", self.do_client_join)
      self.register_command("drop", self.do_client_drop)
      self.register_command("d", self.do_client_drop)
      self.register_command("hush", self.do_client_drop)
      self.register_command("h", self.do_client_help)
      self.register_command("help", self.do_client_help)
      self.register_command("die", self.do_client_stop_everything)
      self.register_command("D", self.do_client_stop_everything)

      self.cfg = cfg

      self.filter_prototypes = {}

   def register_command(self, cmdname, cmd):
      #assert type(cmd) == function
      if cmdname not in self.client_commands:
         self.client_commands[cmdname] = cmd
      else:
         logging.warning("Note: Attempt to overwrite command `{}' failed".format(cmdname))

   def register_filter(self, name, impl):
      #assert exists impl.from_client, "Error: `{}' filter implementation needs from_client()".format(name)
      #assert exists impl.from_server, "Error: `{}' filter implementation needs from_server()".format(name)

      if name not in self.filter_prototypes:
         self.filter_prototypes[name] = impl
      else:
         logging.warning("Note: Attempted to overwrite filter type `{}' failed".format(name))

   def wall(self, mesg):
      """Warn every client with the string `mesg'."""
      for socket in self.client_sockets:
         assert socket in self.socket_wrappers
         assert type(self.socket_wrappers[socket]) is LocalClient

         c = self.socket_wrappers[socket]
         c.tell_err(mesg)

   ###
   ### STATE: server
   ###

   def handle_line_server(self, socket, line):
      assert socket in self.server_sockets

      svr = self.socket_wrappers[socket]
      ln = line

      for f in svr.filters:
         try:
            ln = f.from_server(ln)
         except Exception:
            kind, value, t = sys.exc_info()
            logging.error("Error applying a server filter: {}".format(repr(value)))
            logging.error(traceback.format_exc())

         if ln is None:
            return

      self.socket_wrappers[socket].handle_data(ln)
      return False # don't continue trying states

   def do_start_connection(self, server):
      logging.info("Starting to connect to server {}:{}.".format(server.host, server.port))

      # This will always be ran in a thread -- to prevent long-blocking connection
      # attempts from hanging the whole program (e.g., when a server is down,
      # tinyfugue can spend quite a while waiting for a connection attempt to
      # come through...)

      # The main program will set connecting_in_thread synchronously *before*
      # calling this thread, so we don't need to worry about accidental multiple
      # connection attempts.

      # It would probably be better to try to figure out asynchronous connect() or
      # something eventually.

      try:
         assert type(server) == RemoteServer
         assert server.socket == None
         assert not server.connected

         rlock = False

         C = socket.create_connection((server.host, server.port))

         if server.use_SSL:
            C = self.tls_ctx_remote.wrap_socket(C)

         self.LOCK.acquire()
         rlock = True

         server.attach_socket(C)
         self.socket_wrappers[C] = server
         self.server_sockets += [C]
         self.sel.register(C, selectors.EVENT_READ)

      except ConnectionRefusedError:
         server.warn_all("Connection attempt failed: Connection refused")

      except ssl.SSLError as e:
         server.warn_all("Connection attempt failed, SSL error: {}", repr(e))

      except (socket.error, socket.herror, socket.gaierror, socket.timeout) as err:
         server.warn_all("Connection attempt failed, network error: {}".format(repr(err)))

      except OSError as err:
         server.warn_all("Connection attempt failed, OSError: {}".format(repr(err)))

      except Exception:
         kind, value, t = sys.exc_info()
         server.warn_all("Connection attempt failed, other error: {}".format(repr(value)))
         logging.error("NON-SOCKET CONNECTION ERROR\n===========================\n\n" + traceback.format_exc())

      finally:
         server.connecting_in_thread = False
         if rlock:
            self.LOCK.release()
         return

   def start_connection(self, server):
      assert type(server) == RemoteServer

      if not server.connecting_in_thread and not server.connected:
         server.connecting_in_thread = True
         t_connect = threading.Thread(target = self.do_start_connection, args = [server])
         t_connect.start()
         return True
      else:
         return False

   ###
   ### STATE: unauthenticated client
   ###

   def handle_line_auth(self, socket, line):
      assert socket in self.unauthenticated_sockets

      s = line.as_str().replace('\r\n', '').replace('\n', '')

      if self.password.verify(s):
         if cfg.get("warn_about_connections", True):
            self.wall("A client has authorized itself.")

         while socket in self.unauthenticated_sockets:
            self.unauthenticated_sockets.remove(socket)

         self.client_sockets.append(socket)

      else:
         c = self.socket_wrappers[socket]
         c.tell_err("Incorrect.")

      return True # stop the main loop from going on to state n+1


   ###
   ### STATE: client
   ###

   def handle_line_client(self, socket, line):
      assert socket in self.client_sockets

      c = self.socket_wrappers[socket]

      ln = line
      for f in c.filters:
         try:
            ln = f.from_client(ln)
         except Exception:
            kind, value, t = sys.exc_info()
            logging.error("Error applying a client filter: {}".format(repr(value)))
            logging.error(traceback.format_exc())

         if ln is None:
            return

      s = line.as_str().replace('\r\n', '').replace('\n', '')

      if s[:len(COMMAND_PREFIX)] == COMMAND_PREFIX:
         try:
            if ' ' in s:
               cmd = s[len(COMMAND_PREFIX):s.index(' ')]
               args = s[s.index(' ')+1:]
            else:
               cmd = s[len(COMMAND_PREFIX):]
               args = ''

            if cmd in self.client_commands:
               self.client_commands[cmd](args, c)
            else:
               c.tell_err("Command `{}' not found.".format(cmd))

         except Exception:
            kind, value, t = sys.exc_info()
            c.tell_err("Error during command processing: {}".format(repr(value)))
            logging.error("COMMAND PROCESSING ERROR\n========================\n\n" + traceback.format_exc())

      else:
         c.handle_data(line)

      return False # don't continue trying states

   def do_client_join(self, args, client):
      """Start listening to a server."""
      assert type(client) == LocalClient

      if args in self.servers:
         if client.subscribedTo is not None:
            client.unsubscribe()
         self.servers[args].subscribe(client)
         client.subscribe(self.servers[args])
         client.tell_ok("Subscribed to server `{}'.".format(args))
         return True
      else:
         client.tell_err("No such server `{}'.".format(args))
         return False

   def do_client_connect(self, args, client):
      """Start listening to a server and initiate a connection to it."""
      assert type(client) == LocalClient

      if self.do_client_join(args, client):
         self.start_connection(self.servers[args])
         return True #(ish)
      else:
         return False

   def do_client_drop(self, args, client):
      """Stop listening to the server, but without actually closing the connection to it."""
      assert type(client) == LocalClient

      try:
         client.unsubscribe()
         client.tell_ok("Stopped listening.")
      except ValueError:
         client.tell_err("Couldn't stop listening to this; it may be silent already.")

   def do_client_debug(self, args, client):
      """Supply a Python expression to eval() for debugging purposes."""
      assert type(client) == LocalClient

      try:
         client.tell_ok(repr(eval(args)))
      except Exception:
         kind, value, t = sys.exc_info()
         client.tell_err(repr(value))

   def do_client_help(self, args, client):
      """Get help."""
      assert type(client) == LocalClient

      # Commands can have multiple names, so we collect them in a dictionary ordered by
      # the function they call so that we don't end up displaying the same bit of help
      # many times.
      cmds = {}

      for cmd, fn in self.client_commands.items(): # (k, v)
         if fn in cmds:
            cmds[fn].append(cmd)
         else:
            cmds[fn] = [cmd]

      for fn, names in cmds.items(): # (k, v)
         client.tell_ok("{}: {}".format(', '.join(names), fn.__doc__ or "No documentation provided."))

   def do_client_stop_everything(self, args, client):
      """Stop the proxy."""
      # This is kind of stupid, isn't it?
      raise KeyboardInterrupt()

   ###
   ### MAIN LOOP
   ###

   def run(self):
      # I'm not sure how much sense it makes to do this here and not in __init__ but oh well.
      for name, proto in self.cfg['servers'].items(): # (k, v)
         self.servers[name] = RemoteServer(proto['host'], proto['port'], name)

         if 'encoding' in proto:
            self.servers[name].encoding = proto['encoding']
         if 'ssl' in proto and proto['ssl'] is True:
            self.servers[name].use_SSL = True

         server_filters = self.cfg.get('filter_servers', [])
         try:
            self.servers[name].add_filters(server_filters, self.filter_prototypes)
            self.servers[name].add_filters(proto.get('filters', []), self.filter_prototypes)
         except FilterSpecificationError as e:
            logging.error("Error while setting up filters: {}".format(str(e)))

      client_filters = self.cfg.get('filter_clients', [])

      try:
         def do_accept(socket, mask):
            logging.info("Accepting new client...")

            # When SSL is turned on, this can block waiting for the client to send an SSL handshake.
            # Maybe consider running it in a thread, too?  (That's a lot of threading though.  And
            # clients are more under our control than remote servers are.)
            try:
               connection, address = socket.accept() # and hope it works
            except ssl.SSLError as e:
               logging.error("SSL error in do_accept(): {}".format(e))
               return
            except Exception:
               kind, val, traceback = sys.exc_info()
               logging.error("Error in do_accept(): {}".format(val))
               return

            logging.info("Accepted {} from {} (mask={}).".format(repr(connection), repr(address), repr(mask)))

            if cfg.get("warn_about_connections", True):
               self.wall("A client has connected from {}.".format(repr(address)))

            self.socket_wrappers[connection] = LocalClient(connection)
            self.unauthenticated_sockets += [connection]
            self.sel.register(connection, selectors.EVENT_READ)

            try:
               self.socket_wrappers[connection].add_filters(client_filters, self.filter_prototypes)
            except FilterSpecificationError as e:
               self.socket_wrappers[connection].tell_err("Error setting up client filters: {}".format(str(e)))

         server = socket.socket()
         server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

         bind_to_host = self.cfg.get("bind_to_host", BIND_TO_HOST)
         bind_to_port = self.cfg.get("bind_to_port", BIND_TO_PORT)
         if type(bind_to_host) != str:
            logging.error("Error: host to bind to must be a string")
            return
         if type(bind_to_port) != int:
            logging.error("Error: port to bind to must be a string")
            return
         server.bind((bind_to_host, bind_to_port))

         server = self.tls_ctx_local.wrap_socket(server, server_side=True)

         server.listen(100)
         server.setblocking(False)
         self.sel.register(server, selectors.EVENT_READ)

         logging.info("Listening.")

         while True:
            events = self.sel.select(timeout = 1)

            self.LOCK.acquire()

            for key, mask in events:
               s = key.fileobj
               if s == server:
                  do_accept(s, mask)
               else:
                  if s in self.socket_wrappers:
                     ss = self.socket_wrappers[s]
                  else:
                     raise Exception("Read on unregistered socket")
                     break

                  (lines, eof) = ss.read()

                  if eof:
                     self.sel.unregister(s)
                     ss.handle_disconnect()
                     for state in self.states:
                        if s in state[0]:
                           del state[0][state[0].index(s)]
                     if s in self.socket_wrappers:
                        del self.socket_wrappers[s]

                  for line in lines:
                     for state in self.states:
                        if s in state[0]:
                           result = state[1](s, line)
                           if result:
                              break # to next line

            self.LOCK.release()

      except KeyboardInterrupt:
         logging.info("Caught KeyboardInterrupt; quitting...")


###
### STARTUP / initialization
###

if __name__ == '__main__':
   cfg = load_json(CONFIG_FILE)

   if cfg is None:
      logging.error("Must have configuration")
      exit(1)

   proxy = Proxy(cfg)

   pluginDir = cfg.get('plugin_directory', "plugins")
   plugin_err_fatal = cfg.get('plugin_errors_fatal', True)

   plugins = {}
   for P in pkgutil.iter_modules([pluginDir]):
      try:
         plugin = P.name
         m = importlib.import_module("{}.{}".format(pluginDir, plugin))
         m.setup(proxy)
         plugins[plugin] = m
         logging.info("Loaded plugin {}".format(plugin))
      except Exception:
         kind, value, traceback = sys.exc_info()
         logging.error("Error loading plugin {}: {}".format(plugin, repr(value)))
         #print("-------------------- TRACEBACK:")
         #print(traceback.format_exc())
         if plugin_err_fatal:
            raise value

   try:
      proxy.run()

   except Exception:
      kind, value, t = sys.exc_info()
      logging.error("Runtime error: {}".format(repr(value)))
      traceback.print_exc()

   for P in plugins.values():
      try:
         P.teardown(proxy)

      except AttributeError:
         pass

      except Exception:
         kind, value, t = sys.exc_info()
         logging.error("Error unloading plugins: {}".format(repr(value)))
         traceback.print_exc()
