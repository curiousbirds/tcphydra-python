import xmlwriter
import ansi

import datetime
import logging

open_logs = []

class LoggingFilter:
    def __init__(self, connection, options):
        print("Init with options {}".format(repr(options)))
        self.filename_template = options['filename']
        self.filename = None
        self.filehandle = None
        self.xml = None

    def resetfilename(self):
        self.filename = self.filename_template \
                        .replace('DATE', time.strftime("%Y-%m-%d_%H%M"))

    def open(self):
        global open_logs

        if self.filehandle is not None:
            raise ValueError("Cannot open log when already open")

        try:
            self.filehandle = open(self.filename, 'x')
        except FileExistsError:
            logging.error("Can't open logfile {}: it exists".format(self.filename))

        self.xml = xmlwriter.XmlTagOutputter(indent='   ')

        def addtext(text):
            self.filehandle.write(text)

        self.xml.write_callback = addtext
        self.xml.buffer = False

        self.xml.open_tag("log")

        if self not in open_logs:
            open_logs.append(self)

    def close(self):
        global open_logs

        if self.filehandle is None:
            raise ValueError("Cannot close log when already closed")

        self.xml.close_all()
        self.filehandle.close()
        self.filehandle = None
        self.xml = None

        while self in open_logs:
            open_logs.remove(self)

    def from_server(self, line):
        if self.filehandle is None:
            self.open()

        self.xml.open_tag("line", {'date': datetime.datetime.utcnow().isoformat()})

        try:
            line = ansi.parse_ANSI(line.as_str())
        except ansi.ANSIParsingError as e:
            logging.warning("Error while trying to parse ANSI colors: {}".format(str(e)))
            # [1:-1] removes the quotes from the repr() representation, effectively
            # escaping any 'funny' characters that might be in the string.
            # ... this might possibly be not the best solution.
            line = [repr(line.as_str())[1:-1]]

        pending_text = None
        pending_colors = None

        for chunk in line:
            if type(chunk) is dict:
                if pending_colors is not None and pending_text is not None:
                    self.xml.inline_tag("text", pending_colors, pending_text)
                    pending_text = None

                pending_colors = chunk

            elif type(chunk) is str:
                if pending_text is None:
                    pending_text = chunk
                else:
                    pending_text += chunk

            else:
                raise TypeError("Unexpected type in parsed line of text")

        if pending_text is not None:
            self.xml.inline_tag("text", pending_colors, pending_text)

    def from_client(self, line):
        pass

    def server_connect(self, connected):
        if connected:
            self.resetfilename()
            self.open()
        else:
            self.close()

def setup(proxy):
    proxy.register_filter("xlogs", LoggingFilter)

def teardown(proxy):
    for log in open_logs:
        log.close()
