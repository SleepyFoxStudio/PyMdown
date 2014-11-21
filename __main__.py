#!/usr/bin/env python
"""
PyMdown  CLI

Front end CLI that allows the batch conversion of
markdown files to HTML.  Also accepts an input stream
for piping markdown.

Licensed under MIT
Copyright (c) 2014 Isaac Muse <isaacmuse@gmail.com>
"""
from __future__ import unicode_literals
from __future__ import absolute_import
from __future__ import print_function

# Egg resoures must be loaded before Pygments gets loaded
import lib.resources as res
res.load_egg_resources()

import codecs
import sys
import subprocess
import webbrowser
import traceback
import json
import os.path as path
from lib import logger
from lib import settings
from lib import critic_dump
from lib import formatter
from lib.mdconvert import MdConverts
from lib.frontmatter import get_frontmatter_string
try:
    from lib import scrub
    scrublib = True
except:
    scrublib = False

__version_info__ = (0, 7, 0)
__version__ = '.'.join(map(str, __version_info__))

PY3 = sys.version_info >= (3, 0)

if sys.platform.startswith('win'):
    _PLATFORM = "windows"
elif sys.platform == "darwin":
    _PLATFORM = "osx"
else:
    _PLATFORM = "linux"

PASS = 0
FAIL = 1

script_path = path.dirname(path.abspath(__file__))


def get_files(file_patterns):
    """ Find and return files matching the given patterns """

    import glob
    files = []
    all_files = set()
    assert len(file_patterns), "No file patterns"
    if len(file_patterns):
        for pattern in file_patterns:
            files += glob.glob(pattern)
    for f in files:
        all_files.add(path.abspath(path.normpath(f)))
    return list(all_files)


def get_file_stream(encoding):
    """ Get the file stream """

    import fileinput
    import traceback
    sys.argv = []
    text = []
    try:
        for line in fileinput.input():
            text.append(line if PY3 else line.decode(encoding))
        stream = ''.join(text)
    except:
        logger.Log.error(traceback.format_exc())
        stream = None
    text = None
    return stream


def auto_open(name):
    """ Auto open HTML """

    # Here is an attempt to load the HTML in the
    # the default browser.  I guess if this doesn't work
    # I could always just inlcude the desktop lib.
    if _PLATFORM == "osx":
        web_handler = None
        try:
            launch_services = path.expanduser('~/Library/Preferences/com.apple.LaunchServices.plist')
            with open(launch_services, "rb") as f:
                content = f.read()
            args = ["plutil", "-convert", "json", "-o", "-", "--", "-"]
            p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            p.stdin.write(content)
            out, err = p.communicate()
            plist = json.loads(out.decode('utf-8') if PY3 else out)
            for handler in plist['LSHandlers']:
                if handler.get('LSHandlerURLScheme', '') == "http":
                    web_handler = handler.get('LSHandlerRoleAll', None)
                    break
        except:
            pass
        if web_handler is not None:
            subprocess.Popen(['open', '-b', web_handler, name])
        else:
            subprocess.Popen(['open', name])
    elif _PLATFORM == "windows":
        webbrowser.open(name, new=2)
    else:
        try:
            # Maybe...?
            subprocess.Popen(['xdg-open', name])
        except OSError:
            webbrowser.open(name, new=2)
            # Well we gave it our best...


def get_critic_mode(args):
    """ Setp the critic mode """
    critic_mode = settings.CRITIC_IGNORE
    if args.accept and args.reject:
        critic_mode |= settings.CRITIC_VIEW
    elif args.accept:
        critic_mode |= settings.CRITIC_ACCEPT
    elif args.reject:
        critic_mode |= settings.CRITIC_REJECT
    if args.critic_dump:
        critic_mode |= settings.CRITIC_DUMP
    return critic_mode


def get_references(references, basepath, encoding):
    """ Get footnote and general references from outside source """

    text = ''
    for file_name in references:
        file_name, encoding = res.splitenc(file_name, default=encoding)
        user_path = path.join(res.get_user_path(), file_name)

        is_abs = res.is_absolute(file_name)

        # Find path relative to basepath or global user path
        # If basepath is defined set paths relative to the basepath if possible
        # or just keep the absolute
        if not is_abs:
            # Is relative path
            base_temp = path.normpath(path.join(basepath, file_name)) if basepath is not None else None
            user_temp = path.normpath(path.join(user_path, file_name)) if user_path is not None else None
            if base_temp is not None and path.exists(base_temp) and path.isfile(base_temp):
                ref_path = base_temp
            elif user_temp is not None and path.exists(user_temp) and path.isfile(user_temp):
                ref_path = user_temp
            else:
                # Is an unknown path
                ref_path = None
        elif is_abs and path.exists(file_name) and path.isfile(file_name):
            # Is absolute path
            ref_path = file_name
        else:
            # Is an unknown path
            ref_path = None

        if ref_path is not None:
            text += res.load_text_resource(ref_path, encoding=encoding)
        else:
            logger.Log.error("Could not find reference file %s!" % file_name)
    return text


def get_sources(args):
    """ Get file(s) or stream """
    files = None
    stream = False

    try:
        files = get_files(args.markdown)
    except:
        files = [get_file_stream(args.encoding)]
        stream = True
    return files, stream


def get_title(file_name, title_val):
    """ Get title for HTML """
    if title_val is not None:
        title = str(title_val)
    elif file_name is not None:
        title = path.splitext(path.basename(path.abspath(file_name)))[0]
    else:
        title = None
    return title


def merge_meta(meta1, meta2, title=None):
    """ Merge meta1 and meta2.  Add title to meta data if needed. """
    meta = meta1.copy()
    meta.update(meta2)
    if "title" not in meta and title is not None:
        if title is not None:
            meta["title"] = title
    return meta


def display_licenses():
    """ Display licenses """
    status = PASS
    text = res.load_text_resource(path.join('data', 'licenses.txt'), internal=True)
    if text is not None:
        if PY3:
            sys.stdout.buffer.write(text.encode('utf-8'))
        else:
            sys.stdout.write(text.encode('utf-8'))
    else:
        status = FAIL
    return status


class Convert(object):
    def __init__(
        self, config,
        output=None, basepath=None,
        title=None, settings_path=None
    ):
        self.config = config
        self.output = output
        self.basepath = basepath
        self.title = title
        self.settings_path = settings_path

    def strip_frontmatter(self, text):
        """
        Extract settings from file frontmatter and config file
        Fronmatter options will overwrite config file options.
        """

        frontmatter, text = get_frontmatter_string(text)
        return frontmatter, text

    def get_file_settings(self, file_name, frontmatter={}):
        """
        Using the filename and/or frontmatter, this retrieves
        the full set of settings for this specific file.
        """

        status = PASS
        try:
            self.settings = self.config.get(
                file_name, basepath=self.basepath, output=self.output, frontmatter=frontmatter
            )
        except:
            logger.Log.error(traceback.format_exc())
            status = FAIL
        return status

    def read_file(self, file_name):
        """ Read the source file """
        try:
            with codecs.open(file_name, "r", encoding=self.config.encoding) as f:
                text = f.read()
        except:
            logger.Log.error("Failed to open %s!" % file_name)
            text = None
        return text

    def critic_dump(self, file_name, text):
        """ Dump the markdown back out after stripping critic marks """
        status = PASS

        status = self.get_file_settings(file_name)

        if status == PASS and file_name is not None:
            text = self.read_file(file_name)
            if text is None:
                status = FAIL

        if status == PASS:
            if not (self.config.critic & (settings.CRITIC_REJECT | settings.CRITIC_ACCEPT)):
                logger.Log.error("Acceptance or rejection was not specified for critic dump!")
                status = FAIL
            else:
                # Create text object
                try:
                    txt = formatter.Text(self.settings["page"]["destination"], self.config.output_encoding)
                except:
                    logger.Log.error("Failed to create text file!")
                    status = FAIL

        if status == PASS:
            text = critic_dump.CriticDump().dump(
                text,
                self.config.critic & settings.CRITIC_ACCEPT,
                self.config.critic & settings.CRITIC_VIEW
            )
            txt.write(text)
            txt.close()

        return status

    def html_dump(self, file_name, text):
        """ Convet markdown to HTML """
        status = PASS

        html_title = get_title(file_name, self.title)

        if status == PASS and file_name is not None:
            text = self.read_file(file_name)
            if text is None:
                status = FAIL

        if status == PASS:
            frontmatter, text = self.strip_frontmatter(text)
            status = self.get_file_settings(file_name, frontmatter=frontmatter)

        if status == PASS:
            text += get_references(
                self.settings["page"].get("references", []),
                self.settings["page"]["basepath"],
                self.config.encoding
            )

            # Create html object
            try:
                html = formatter.Html(
                    self.settings["page"]["destination"], preview=self.config.preview,
                    plain=self.config.plain, settings=self.settings["settings"],
                    basepath=self.settings["page"]["basepath"],
                    aliases=self.settings["page"]["include"],
                    encoding=self.config.output_encoding
                )
            except:
                logger.Log.error(traceback.format_exc())
                status = FAIL

        if status == PASS:
            # Convert markdown to HTML
            try:
                converter = MdConverts(
                    text,
                    smart_emphasis=self.settings["settings"].get('smart_emphasis', True),
                    lazy_ol=self.settings["settings"].get('lazy_ol', True),
                    tab_length=self.settings["settings"].get('tab_length', 4),
                    base_path=self.settings["page"]["basepath"],
                    relative_output=path.dirname(html.file.name) if html.file.name else self.config.relative_out,
                    enable_attributes=self.settings["settings"].get('enable_attributes', True),
                    output_format=self.settings["settings"].get('output_format', 'xhtml1'),
                    extensions=self.settings["settings"]["extensions"]
                )

                converter.convert()

                # Retrieve meta data if available and merge with frontmatter
                # meta data.
                #     1. Frontmatter will override normal meta data.
                #     2. Meta data overrides --title option on command line.
                html.set_meta(
                    merge_meta(converter.meta, self.settings["page"]["meta"], title=html_title)
                )
            except:
                logger.Log.error(str(traceback.format_exc()))
                html.close()
                status = FAIL

        if status == PASS:
            # Write the markdown
            html.write(scrub.scrub(converter.markdown) if self.config.clean else converter.markdown)
            html.close()

            # Preview the markdown
            if html.file.name is not None and self.config.preview and status == PASS:
                auto_open(html.file.name)
        return status

    def convert(self, files):
        """ Convert markdown file(s) """
        status = PASS

        # Make sure we have something we can process
        if files is None or len(files) == 0 or files[0] in ('', None):
            logger.Log.error("Nothing to parse!")
            status = FAIL

        if status == PASS:
            for md_file in files:
                # Quit dumping if there was an error
                if status != PASS:
                    break

                if self.config.is_stream:
                    logger.Log.info("Converting buffer...")
                else:
                    logger.Log.info("Converting %s..." % md_file)

                if status == PASS:
                    file_name = md_file if not self.config.is_stream else None
                    text = None if not self.config.is_stream else md_file
                    md_file = None
                    if self.config.critic & settings.CRITIC_DUMP:
                        status = self.critic_dump(file_name, text)
                    else:
                        status = self.html_dump(file_name, text)
        return status


if __name__ == "__main__":
    import argparse

    def main():
        """ Go! """

        parser = argparse.ArgumentParser(prog='pymdown', description='Markdown generator')
        # Flag arguments
        parser.add_argument('--version', action='version', version="%(prog)s " + __version__)
        parser.add_argument('--debug', '-d', action='store_true', default=False, help=argparse.SUPPRESS)
        parser.add_argument('--licenses', action='store_true', default=False, help="Display licenses.")
        parser.add_argument('--quiet', '-q', action='store_true', default=False, help="No messages on stdout.")
        # Format and Viewing
        parser.add_argument('--preview', '-p', action='store_true', default=False, help="Output to preview (temp file). --output will be ignored.")
        parser.add_argument('--plain-html', '-P', action='store_true', default=False, help="Strip out CSS, style, ids, etc.  Just show tags.")
        parser.add_argument('--title', default=None, help="Title for HTML.")
        # Critic features
        parser.add_argument('--accept', '-a', action='store_true', default=False, help="Accept propossed critic marks when using in normal processing and --critic-dump processing")
        parser.add_argument('--reject', '-r', action='store_true', default=False, help="Reject propossed critic marks when using in normal processing and --critic-dump processing")
        parser.add_argument('--critic-dump', action='store_true', default=False, help="Process critic marks, dumps file(s), and exits.")
        # Output
        parser.add_argument('--output', '-o', default=None, help="Output file. Ignored in batch mode.")
        parser.add_argument('--batch', '-b', action='store_true', default=False, help="Batch mode output.")
        parser.add_argument('--force-stdout', action='store_true', default=False, help="Force output to stdout.")
        parser.add_argument('--force-no-template', action='store_true', default=False, help="Force using no template.")
        parser.add_argument('--output-encoding', '-E', default=None, help="Output encoding.")
        parser.add_argument('--clean', '-c', action='store_true', default=False, help=argparse.SUPPRESS)
        # Input configuration
        parser.add_argument('--settings', '-s', default=path.join(res.get_user_path(), "pymdown.cfg"), help="Load the settings file from an alternate location.")
        parser.add_argument('--encoding', '-e', default="utf-8", help="Encoding for input.")
        parser.add_argument('--basepath', default=None, help="The basepath location pymdown should use.")
        parser.add_argument('markdown', nargs='*', default=[], help="Markdown file(s) or file pattern(s).")

        args = parser.parse_args()

        if args.licenses:
            return display_licenses()

        if args.debug:
            logger.Log.set_level(logger.CRITICAL if args.quiet else logger.DEBUG)
        else:
            logger.Log.set_level(logger.CRITICAL if args.quiet else logger.INFO)

        files, stream = get_sources(args)
        if stream:
            batch = False
        else:
            batch = args.batch

        if not batch and len(files) > 1:
            logger.Log.log("Please use batch mode to process multiple files!")
            return FAIL

        # It is assumed that the input encoding is desired for output
        # unless otherwise specified.
        if args.output_encoding is None:
            args.output_encoding = args.encoding

        # Unpack user files if needed
        res.unpack_user_files()

        # Setup config class
        config = settings.Settings(
            encoding=args.encoding,
            output_encoding=args.output_encoding,
            critic=get_critic_mode(args),
            batch=batch,
            stream=stream,
            preview=args.preview,
            settings_path=args.settings,
            plain=args.plain_html,
            force_stdout=args.force_stdout,
            force_no_template=args.force_no_template,
            clean=(args.clean if scrublib else False)
        )
        config.read_settings()

        # Convert
        return Convert(
            basepath=args.basepath,
            title=args.title,
            output=args.output,
            config=config
        ).convert(files)

    script_path = path.dirname(path.abspath(sys.argv[0]))

    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
