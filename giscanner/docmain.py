# -*- Mode: Python -*-
# GObject-Introspection - a framework for introspecting GObject libraries
# Copyright (C) 2008-2011 Johan Dahlin
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.
#

import os
import argparse

from .docwriter import DocWriter
from .sectionparser import generate_sections_file, write_sections_file
from .transformer import Transformer
from . import message


def doc_main(args):
    logger = message.MessageLogger.get(namespace=None)
    logger.enable_warnings((message.WARNING, message.ERROR, message.FATAL))

    parser = argparse.ArgumentParser()

    parser.add_argument("girfile")
    parser.add_argument("-o", "--output",
                      action="store", dest="output",
                      help="Directory to write output to")
    parser.add_argument("-l", "--language",
                      action="store", dest="language",
                      default="c",
                      help="Output language")
    parser.add_argument("-I", "--add-include-path",
                      action="append", dest="include_paths", default=[],
                      help="include paths for other GIR files")
    parser.add_argument("-M", "--markdown-include-path",
                      action="append", dest="markdown_include_paths", default=[],
                      help="include paths for markdown inclusion")
    parser.add_argument("-s", "--write-sections-file",
                      action="store_true", dest="write_sections",
                      help="Generate and write out a sections file")
    parser.add_argument("-u", "--sections-file",
                      action="store", dest="sections_file",
                      help="Sections file to use for ordering")
    parser.add_argument("-O", "--online-links",
                      action="store_true", dest="online_links",
                      help="Generate online links")
    parser.add_argument("-g", "--link-to-gtk-doc",
                      action="store_true", dest="link_to_gtk_doc",
                      help="Link to gtk-doc documentation, the documentation "
                      "packages to link against need to be installed in "
                      "/usr/share/gtk-doc")
    parser.add_argument("-R", "--resolve-implicit-links",
                      action="store_true", dest="resolve_implicit_links",
                      help="All the space and parentheses-separated tokens "
                      "in the comment blocks will be analyzed to see if they "
                      "map to an existing code or symbol. If they do, a link "
                      "will be inserted, for example 'pass that function "
                      "a GList' will resolve the existing GList type and "
                      "insert a link to its documentation")

    args = parser.parse_args(args[1:])
    if not args.output:
        raise SystemExit("missing output parameter")

    if 'UNINSTALLED_INTROSPECTION_SRCDIR' in os.environ:
        top_srcdir = os.environ['UNINSTALLED_INTROSPECTION_SRCDIR']
        top_builddir = os.environ['UNINSTALLED_INTROSPECTION_BUILDDIR']
        extra_include_dirs = [os.path.join(top_srcdir, 'gir'), top_builddir]
    else:
        extra_include_dirs = []
    extra_include_dirs.extend(args.include_paths)
    transformer = Transformer.parse_from_gir(args.girfile, extra_include_dirs)

    if args.write_sections:
        sections_file = generate_sections_file(transformer)

        fp = open(args.output, 'w')
        write_sections_file(fp, sections_file)
        fp.close()
    else:
        writer = DocWriter(transformer, args.language,
                args.markdown_include_paths, online=args.online_links,
                link_to_gtk_doc=args.link_to_gtk_doc,
                resolve_implicit_links=args.resolve_implicit_links,
                sections_file=args.sections_file)
        writer.write(args.output)

    return 0
