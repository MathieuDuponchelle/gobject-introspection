#!/usr/bin/env python
# -*- Mode: Python -*-
# GObject-Introspection - a framework for introspecting GObject libraries
# Copyright (C) 2010 Zach Goldberg
# Copyright (C) 2011 Johan Dahlin
# Copyright (C) 2011 Shaun McCance
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
import re
import tempfile

from xml.sax import saxutils
from xml.etree import ElementTree as ET
from mako.lookup import TemplateLookup

from . import message
from . import ast, xmlwriter
from .utils import to_underscores, indent

# Freely inspired from
# https://github.com/GNOME/yelp-xsl/blob/master/js/syntax.html
language_mimes = {
    "bash-script": "application/x-shellscript",
    "shell": "application/x-shellscript",
    "csharp": "text/x-csharp",
    "css": "text/css",
    "diff": "text/xpatch",
    "html": "text/html",
    "java": "text/x-java",
    "javascript": "application/javascript",
    "lisp": "text/x-scheme",
    "lua": "text-x-lua",
    "c": "text/x-csrc",
    "c++": "text/x-c++src",
    "pascal": "text/x-pascal",
    "perl": "application/x-perl",
    "php": "application/x-php",
    "python": "text/x-python",
    "ruby": "application/x-ruby",
    "sql": "text/x-sql",
    "yaml": "application/x-yaml",
}


def make_page_id(node, recursive=False):
    if isinstance(node, ast.Namespace):
        if recursive:
            return node.name
        else:
            return 'index'

    if hasattr(node, '_chain') and node._chain:
        parent = node._chain[-1]
    else:
        parent = getattr(node, 'parent', None)

    if parent is None:
        if isinstance(node, ast.Function) and node.shadows:
            return '%s.%s' % (node.namespace.name, node.shadows)
        else:
            return '%s.%s' % (node.namespace.name, node.name)

    if isinstance(node, (ast.Property, ast.Signal, ast.VFunction, ast.Field)):
        return '%s-%s' % (make_page_id(parent, recursive=True), node.name)
    elif isinstance(node, ast.Function) and node.shadows:
        return '%s.%s' % (make_page_id(parent, recursive=True), node.shadows)
    else:
        return '%s.%s' % (make_page_id(parent, recursive=True), node.name)


def make_gtkdoc_id(node, separator=None, formatter=None):
    def class_style(name):
        return name

    def function_style(name):
        snake_case = re.sub('(.)([A-Z][a-z]+)', r'\1_\2',
                name)
        snake_case = re.sub('([a-z0-9])([A-Z])', r'\1_\2', snake_case).lower()
        return snake_case.replace("_", "-")

    if separator is None:
        separator = "-"
        formatter = function_style
        if isinstance(node, (ast.Class, ast.Enum, ast.Record, ast.Interface,
                             ast.Callback, ast.Alias)):
            separator = ""
            formatter = class_style

    if isinstance(node, ast.Namespace):
        return formatter(node.identifier_prefixes[0])

    if hasattr(node, '_chain') and node._chain:
        parent = node._chain[-1]
    else:
        parent = getattr(node, 'parent', None)

    if parent is None:
        if isinstance(node, ast.Function) and node.shadows:
            return '%s%s%s' % (formatter(node.namespace.name), separator,
                    formatter(node.shadows))
        else:
            return '%s%s%s' % (formatter(node.namespace.name), separator,
                    formatter(node.name))

    if isinstance(node, ast.Function) and node.shadows:
        return '%s%s%s' % (make_gtkdoc_id(parent, separator=separator,
            formatter=formatter), separator, formatter(node.shadows))
    else:
        return '%s%s%s' % (make_gtkdoc_id(parent, separator=separator,
            formatter=formatter), separator, formatter(node.name))


def get_node_kind(node):
    if isinstance(node, ast.Namespace):
        node_kind = 'namespace'
    elif isinstance(node, (ast.Class, ast.Boxed, ast.Compound)):
        node_kind = 'class'
    elif isinstance(node, ast.Interface):
        node_kind = 'interface'
    elif isinstance(node, ast.Record):
        node_kind = 'record'
    elif isinstance(node, ast.Function):
        if node.is_method:
            node_kind = 'method'
        elif node.is_constructor:
            node_kind = 'constructor'
        else:
            node_kind = 'function'
    elif isinstance(node, (ast.Enum, ast.Bitfield)):
        node_kind = 'enum'
    elif isinstance(node, ast.Property) and node.parent is not None:
        node_kind = 'property'
    elif isinstance(node, ast.Signal) and node.parent is not None:
        node_kind = 'signal'
    elif isinstance(node, ast.VFunction) and node.parent is not None:
        node_kind = 'vfunc'
    elif isinstance(node, ast.Callable):
        node_kind = 'callback'
    elif isinstance(node, ast.Field):
        node_kind = 'field'
    elif isinstance(node, ast.DocSection):
        node_kind = 'docsection'
    else:
        node_kind = 'default'

    return node_kind


class TemplatedScanner(object):
    def __init__(self, specs):
        self.specs = self.unmangle_specs(specs)
        self.regex = self.make_regex(self.specs)

    def unmangle_specs(self, specs):
        mangled = re.compile('<<([a-zA-Z_:]+)>>')
        specdict = dict((name.lstrip('!'), spec) for name, spec in specs)

        def unmangle(spec, name=None):
            def replace_func(match):
                child_spec_name = match.group(1)

                if ':' in child_spec_name:
                    pattern_name, child_spec_name = child_spec_name.split(':', 1)
                else:
                    pattern_name = None

                child_spec = specdict[child_spec_name]
                # Force all child specs of this one to be unnamed
                unmangled = unmangle(child_spec, None)
                if pattern_name and name:
                    return '(?P<%s_%s>%s)' % (name, pattern_name, unmangled)
                else:
                    return unmangled

            return mangled.sub(replace_func, spec)

        return [(name, unmangle(spec, name)) for name, spec in specs]

    def make_regex(self, specs):
        regex = '|'.join('(?P<%s>%s)' % (name, spec) for name, spec in specs
                         if not name.startswith('!'))
        return re.compile(regex)

    def get_properties(self, name, match):
        groupdict = match.groupdict()
        properties = {name: groupdict.pop(name)}
        name = name + "_"
        for group, value in groupdict.iteritems():
            if group.startswith(name):
                key = group[len(name):]
                properties[key] = value
        return properties

    def scan(self, text):
        pos = 0
        while True:
            match = self.regex.search(text, pos)
            if match is None:
                break

            start = match.start()
            if start > pos:
                yield ('other', text[pos:start], None)

            pos = match.end()
            name = match.lastgroup
            yield (name, match.group(0), self.get_properties(name, match))

        if pos < len(text):
            yield ('other', text[pos:], None)


class DocstringScanner(TemplatedScanner):
    def __init__(self):
        specs = [
            ('!alpha', r'[a-zA-Z0-9_]+'),
            ('!alpha_dash', r'[a-zA-Z0-9_-]+'),
            ('!anything', r'.*'),
            ('note', r'\n+\>\s*<<note_contents:anything>>\s*\n'),
            ('new_paragraph', r'\n\n'),
            ('new_line', r'\n'),
            ('code_start_with_language',
                r'\|\[\<!\-\-\s*language\s*\=\s*\"<<language_name:alpha>>\"\s*\-\-\>'),
            ('code_start', r'\|\['),
            ('code_end', r'\]\|'),
            ('property', r'#<<type_name:alpha>>:(<<property_name:alpha_dash>>)'),
            ('signal', r'#<<type_name:alpha>>::(<<signal_name:alpha_dash>>)'),
            ('type_name', r'#(<<type_name:alpha>>)'),
            ('enum_value', r'%(<<member_name:alpha>>)'),
            ('parameter', r'@<<param_name:alpha>>'),
            ('function_call', r'<<symbol_name:alpha>>\(\)'),
            ('include', r'{{\s*<<include_name:anything>>\s*}}'),
            ('heading', r'#+\s+<<heading:anything>>'),
        ]

        super(DocstringScanner, self).__init__(specs)


class HierarchyClass:

    def __init__(self, name, node=None):
        self.parents = []
        self.children = []
        self.name = name
        self.node = node

    def add_parent(self, parent):
        self.parents.append(parent)
        parent.add_child(self)

    def add_child(self, child):
        self.children.append(child)


class Section:

    def __init__(self, name, node, global_table):
        self.subsections = {}
        self.symbols = {}
        self.name = name
        self.next_section = None
        prev_node = None
        for n in node.find("SYMBOLS"):
            title = n.text
            symbol = Symbol(title)
            if prev_node:
                prev_node.set_next(symbol)
            self.symbols[title] = symbol
            prev_node = symbol
            global_table[title] = symbol

    def set_next(self, section):
        self.next_section = section


class Symbol:

    def __init__(self, name):
        self.name = name
        self.next_ = None

    def set_next(self, symbol):
        self.next_ = symbol


class DocFormatter(object):
    def __init__(self, transformer, markdown_include_paths, link_to_gtk_doc,
            online, resolve_implicit_links, sections_file):
        self.online = online
        self.link_to_gtk_doc = link_to_gtk_doc
        self.resolve_implicit_links = resolve_implicit_links
        self._transformer = transformer
        self._scanner = DocstringScanner()
        self.global_symbols_table = {}
        self.sections = self._parse_sections_file(sections_file)

        # If we are processing a code block as defined by
        # https://wiki.gnome.org/Projects/GTK%2B/DocumentationSyntax/Markdown
        # we won't insert paragraphs and will respect new lines.
        self._processing_code = False
        # Support including text files through "{{ }}" blocks
        self._include_directories = markdown_include_paths
        # Support external references
        self._reference_map = dict({})
        self._fill_reference_map(online)
        # Avoid warning multiple times for external links we're not sure about
        self._warned_external_references = []
        # Support Headings
        self._opened_sections = 0

    def _parse_sections_file(self, sections_file):
        if not sections_file:
            return None
        sections = {}
        tree = ET.parse(sections_file)
        root = tree.getroot()
        previous_section = None
        for n in root:
            title = n.find("TITLE").text
            section = Section(title, n, self.global_symbols_table)
            sections[title] = section
            if previous_section:
                previous_section.set_next(section)
            previous_section = section
        return sections

    def _fill_reference_map(self, online):
        if not os.path.exists(os.path.join(DATADIR, "gtk-doc", "html")):
            return

        for node in os.listdir(os.path.join(DATADIR, "gtk-doc", "html")):
            dir_ = os.path.join(DATADIR, "gtk-doc/html", node)
            if os.path.isdir(dir_):
                try:
                    online_reference, symbol_map = self._parse_sgml_index(dir_)
                    self._reference_map[node] = (dir_, online_reference,
                            symbol_map)
                except IOError:
                    online_reference = None

    def _parse_sgml_index(self, dir_):
        online_reference = None
        symbol_map = dict({})
        with open(os.path.join(dir_, "index.sgml"), 'r') as f:
            for l in f:
                if l.startswith("<ONLINE"):
                    online_reference = l.split('"')[1]
                elif l.startswith("<ANCHOR"):
                    split_line = l.split('"')
                    symbol_map[split_line[1]] = split_line[3]
        return online_reference, symbol_map

    def escape(self, text):
        return saxutils.escape(text)

    def should_render_node(self, node):
        if getattr(node, "private", False):
            return False
        # Nodes without namespace are AST bugs really
        # They are used for structs and unions declared
        # inline inside other structs, but they are not
        # even picked up by g-ir-compiler, because they
        # don't create a <type/> element.
        # So just ignore them.
        if isinstance(node, ast.Node) and node.namespace is None:
            return False

        return True

    def format(self, node, doc):
        if doc is None:
            return ''

        result = ""
        processing_code = self._processing_code

        if not processing_code:
            result += '<p>'
        result += self.format_inline(node, doc)
        if not processing_code:
            result += '</p>'

        while self._opened_sections > 0:
            result += "</section>"
            self._opened_sections -= 1
        return result

    def format_xref_from_identifier(self, identifier):
        result = ""
        node = self._resolve_symbol(identifier)

        if not node:
            node = self._resolve_type(identifier)

        if node:
            result = make_page_id(node)
        return result

    def link_next_doc(self, node):
        result = ""
        if isinstance(node, ast.Function):
            try:
                symbol = self.global_symbols_table[node.symbol]
                next_symbol = symbol.next_
                if symbol.next_:
                    result = '<link xref="'
                    result += self.format_xref_from_identifier(symbol.next_.name)
                    result += '" type="next"/>'
            except KeyError:  # Class functions
                pass
        elif self.sections:
            if isinstance(node, (ast.Class, ast.Interface)):
                try:
                    section = self.sections[node.gtype_name]
                    next_section = section.next_section
                    if next_section:
                        result = '<link xref="'
                        result += self.format_xref_from_identifier(next_section.name)
                        result += '" type="next"/>'
                except KeyError:
                    pass
            elif isinstance(node, (ast.DocSection)):
                try:
                    section = self.sections[node.name]
                    next_section = section.next_section
                    if next_section:
                        result = '<link xref="'
                        result += self.format_xref_from_identifier(next_section.name)
                        result += '" type="next"/>'
                except KeyError:
                    pass

        return result

    def _resolve_type(self, ident):
        try:
            matches = self._transformer.split_ctype_namespaces(ident)
        except ValueError:
            return None

        for namespace, name in matches:
            node = namespace.get(name)
            if node:
                return node

        return None

    def _resolve_symbol(self, symbol):
        try:
            matches = self._transformer.split_csymbol_namespaces(symbol)
        except ValueError:
            return None
        for namespace, name in matches:
            node = namespace.get_by_symbol(symbol)
            if node:
                return node

        if not node:
            for namespace, name in matches:
                node = namespace.get(name)
                if node:
                    return node
        return None

    def _find_thing(self, list_, name):
        for item in list_:
            if item.name == name:
                return item
        raise KeyError("Could not find %s" % (name, ))

    def _resolve_implicit_links(self, match):
        match = self.escape(match)
        if not self.resolve_implicit_links:
            return match

        implicit_links = dict({})
        s = re.split(" |\(|\)", match)

        for word in s:
            if not word:
                continue
            type_ = self._resolve_type(word)
            symbol = self._resolve_symbol(word)
            if type_:
                implicit_links[word] = self.format_xref(type_, linkname=word)
            elif symbol:
                implicit_links[word] = self.format_xref(symbol, linkname=word)

        for word, xref in implicit_links.iteritems():
            match = match.replace(word, xref)
        return match

    def _process_other(self, node, match, props):
        return self._resolve_implicit_links(match)

    def _process_property(self, node, match, props):
        type_node = self._resolve_type(props['type_name'])
        if type_node is None:
            return match

        try:
            prop = self._find_thing(type_node.properties, props['property_name'])
        except (AttributeError, KeyError):
            return match

        return self.format_xref(prop, linkname=props['property_name'])

    def _process_signal(self, node, match, props):
        type_node = self._resolve_type(props['type_name'])
        if type_node is None:
            return match

        try:
            signal = self._find_thing(type_node.signals, props['signal_name'])
        except (AttributeError, KeyError):
            return match

        return self.format_xref(signal)

    def _process_type_name(self, node, match, props):
        ident = props['type_name']
        type_ = self._resolve_type(ident)
        plural = False
        if type_ is None:
            singularized = ident.rstrip("s")  # Try to remove plural
            type_ = self._resolve_type(singularized)
            plural = True
            if type_ is None:
                return match

        return self.format_xref(type_, pluralize=plural)

    def _process_enum_value(self, node, match, props):
        member_name = props['member_name']

        try:
            return '<code>%s</code>' % (self.fundamentals[member_name], )
        except KeyError:
            pass

        enum_value = self._resolve_symbol(member_name)
        if enum_value:
            return self.format_xref(enum_value)

        return match

    def _process_parameter(self, node, match, props):
        try:
            parameter = node.get_parameter(props['param_name'])
        except (AttributeError, ValueError):
            return match

        return '<code>%s</code>' % (self.format_parameter_name(node, parameter), )

    def _process_function_call(self, node, match, props):
        func = self._resolve_symbol(props['symbol_name'])
        if func is None:
            return match

        return self.format_xref(func)

    def _process_code_start(self, node, match, props):
        self._processing_code = True
        return "</p><code>"

    def _process_code_start_with_language(self, node, match, props):
        mime = language_mimes[props["language_name"].lower()]
        self._processing_code = True
        if not mime:
            return "</p><code>"
        return '</p><code mime="' + mime + '">'

    def _process_code_end(self, node, match, props):
        self._processing_code = False
        return "</code><p>"

    def _process_new_line(self, node, match, props):
        return '\n'

    def _process_new_paragraph(self, node, match, props):
        if self._processing_code:
            return '\n\n'
        return "</p><p>"

    def _process_include(self, node, match, props):
        filename = props["include_name"].strip()
        f = None

        try:
            f = open(filename, 'r')
        except IOError:
            for dir_ in self._include_directories:
                try:
                    f = open(os.path.join(dir_, filename), 'r')
                    break
                except:
                    continue
        if f:
            contents = f.read()
            if self._processing_code:
                result = self._resolve_implicit_links(contents)
            else:
                result = self.format_inline(node, contents)
            f.close()
        else:
            message.warn("Could not find file %s" % (props["include_name"], ))
            result = match

        return result

    def _process_note(self, node, match, props):
        if self._processing_code:
            return match
        return "</p><note><p>" + props["note_contents"] + "</p></note><p>"

    def _process_heading(self, node, match, props):
        if self._processing_code:
            return match

        result = ""
        match = match.strip("\n")
        header_level = 0
        while match[header_level] == "#":
            header_level += 1

        result += "</p>"
        while self._opened_sections >= header_level:
            result += "</section>"
            self._opened_sections -= 1

        while self._opened_sections < header_level:
            result += "<section>"
            self._opened_sections += 1

        result += "<title>" + props["heading"] + "</title><p>"
        return result

    def _process_token(self, node, tok):
        kind, match, props = tok

        dispatch = {
            'other': self._process_other,
            'property': self._process_property,
            'signal': self._process_signal,
            'type_name': self._process_type_name,
            'enum_value': self._process_enum_value,
            'parameter': self._process_parameter,
            'function_call': self._process_function_call,
            'code_start': self._process_code_start,
            'code_start_with_language': self._process_code_start_with_language,
            'code_end': self._process_code_end,
            'new_line': self._process_new_line,
            'new_paragraph': self._process_new_paragraph,
            'include': self._process_include,
            'note': self._process_note,
            'heading': self._process_heading,
        }

        return dispatch[kind](node, match, props)

    def get_in_parameters(self, node):
        raise NotImplementedError

    def format_inline(self, node, para):
        tokens = self._scanner.scan(para)
        words = [self._process_token(node, tok) for tok in tokens]
        return ''.join(words)

    def format_parameter_name(self, node, parameter):
        if isinstance(parameter.type, ast.Varargs):
            return "..."
        else:
            return parameter.argname

    def format_function_name(self, func):
        raise NotImplementedError

    def format_type(self, type_, link=False):
        raise NotImplementedError

    def format_page_name(self, node):
        if isinstance(node, ast.Namespace):
            return node.name
        elif isinstance(node, ast.Function):
            return self.format_function_name(node)
        elif isinstance(node, ast.Property) and node.parent is not None:
            return '%s:%s' % (self.format_page_name(node.parent), node.name)
        elif isinstance(node, ast.Signal) and node.parent is not None:
            return '%s::%s' % (self.format_page_name(node.parent), node.name)
        elif isinstance(node, ast.VFunction) and node.parent is not None:
            return '%s::%s' % (self.format_page_name(node.parent), node.name)
        elif isinstance(node, ast.Field) and node.parent is not None:
            return '%s->%s' % (self.format_page_name(node.parent), node.name)
        else:
            return make_page_id(node)

    def format_xref(self, node, pluralize=False, linkname=None, **attrdict):
        if node is None or not hasattr(node, 'namespace'):
            attrs = [('xref', 'index')] + attrdict.items()
            return xmlwriter.build_xml_tag('link', attrs, linkname)
        elif isinstance(node, ast.Member) and (self.link_to_gtk_doc is False or
                node.namespace is self._transformer.namespace):
            # Enum/BitField members are linked to the main enum page, except
            # when linking to gtk doc external references
            return self.format_xref(node.parent, linkname=linkname,
                    pluralize=pluralize, **attrdict) + '.' + node.name
        elif node.namespace is self._transformer.namespace:
            return self.format_internal_xref(node, attrdict,
                    linkname=linkname, pluralize=pluralize)
        else:
            return self.format_external_xref(node, attrdict,
                    linkname=linkname, pluralize=pluralize)

    def format_internal_xref(self, node, attrdict, linkname=None, pluralize=False):
        attrs = [('xref', make_page_id(node))] + attrdict.items()
        if linkname:
            return xmlwriter.build_xml_tag('link', attrs, linkname)
        elif not pluralize:
            return xmlwriter.build_xml_tag('link', attrs)
        else:
            return xmlwriter.build_xml_tag('link', attrs, make_page_id(node) +
            "s")

    def _find_reference(self, ns):
        for package in ns.exported_packages:
            try:
                reference = self._reference_map[package]
                return (reference, True)
            except KeyError:
                package = re.sub(r'\-[0-9]+\.[0-9]+$', '', package)
                try:
                    reference = self._reference_map[package]
                    return (reference, False)
                except KeyError:
                    continue

        return ((None, None), False)

    def format_gtk_doc_attributes(self, node, attrdict):
        ns = node.namespace
        attrs = [('href', 'FIXME broken link to %s' % (node.name, ))]
        ref, exact = self._find_reference(ns)

        if not ref[0] and ns.name not in self._warned_external_references:
            message.warn("No reference found for %s" % (ns.name + ns.version, ))
            self._warned_external_references.append(ns.name)
        elif self.online and not ref[1] and \
                ns.name not in self._warned_external_references:
            message.warn("No online reference found for %s" % (ns.name +
                ns.version, ))
            self._warned_external_references.append(ns.name)
        elif not exact and ns.name not in self._warned_external_references:
            message.warn("Using an approximate match for reference %s:%s" %
                    (ns.name + ns.version, ref[0]))
            self._warned_external_references.append(ns.name)

        reference = None
        if ref[0]:
            gtk_doc_identifier = make_gtkdoc_id(node)
            if isinstance(node, (ast.Constant, ast.Member)):
                gtk_doc_identifier = gtk_doc_identifier.upper() + ":CAPS"
            try:
                reference = ref[2][gtk_doc_identifier]
                reference = reference.split("/", 1)[1]
            except KeyError:
                pass

        if self.online and reference:
            attrs = [('href', '%s/%s' % (ref[1], reference))]
        elif reference:
            attrs = [('href', os.path.join(ref[0], reference))]

        return attrs

    def format_external_xref(self, node, attrdict, linkname=None, pluralize=False):
        ns = node.namespace

        if self.link_to_gtk_doc:
            attrs = self.format_gtk_doc_attributes(node, attrdict)
        else:
            attrs = [('href', '../%s-%s/%s.html' % (ns.name, str(ns.version),
                                                    make_page_id(node)))]
        attrs += attrdict.items()
        if linkname:
            return xmlwriter.build_xml_tag('link', attrs, linkname)
        elif not pluralize:
            return xmlwriter.build_xml_tag('link', attrs, self.format_page_name(node))
        else:
            return xmlwriter.build_xml_tag('link', attrs,
                    self.format_page_name(node) + "s")

    def field_is_writable(self, field):
        return True

    def format_property_flags(self, property_, construct_only=False):
        flags = []

        if property_.readable and not construct_only:
            flags.append("Read")
        if property_.writable and not construct_only and \
           self.field_is_writable(property_):
            flags.append("Write")
        if isinstance(property_, ast.Property):
            if property_.construct:
                flags.append("Construct")
            if property_.construct_only:
                flags.append("Construct Only")

        return " / ".join(flags)

    def to_underscores(self, node):
        if isinstance(node, ast.Property):
            return node.name.replace('-', '_')
        elif node.name:
            return to_underscores(node.name)
        elif isinstance(node, ast.Callback):
            return 'callback'
        elif isinstance(node, ast.Union):
            return 'anonymous_union'
        elif isinstance(node, ast.Field):
            return 'anonymous field'
        else:
            raise Exception('invalid node')

    def to_lower_camel_case(self, string):
        return string[0].lower() + string[1:]

    def add_parent_class(self, parent_type, child_class, classes, is_interface=False):
        parent = self._transformer.lookup_typenode(parent_type)
        parent_name = "%s.%s" % (parent.namespace.name, parent.name)
        try:
            parent_class = classes[parent_name]
        except KeyError:
            parent_class = HierarchyClass(parent_name, parent)
        child_class.add_parent(parent_class)
        if not is_interface:
            self.create_hierarchy_classes(parent, parent_class, classes)
        else:
            # Interfaces don't explicitly inherit from GInterface.
            try:
                parent_interface = classes["GObject.GInterface"]
            except KeyError:
                parent_interface = HierarchyClass("GObject.GInterface")
                classes["GObject.GInterface"] = parent_interface
            parent_class.add_parent(parent_interface)

    def create_hierarchy_classes(self, node, child_class, classes):
        name = "%s.%s" % (node.namespace.name, node.name)
        classes[name] = child_class
        parent = None
        if node.parent_type:
            parent = self._transformer.lookup_typenode(node.parent_type)
            self.add_parent_class(node.parent_type, child_class, classes)
        if hasattr(node, "interfaces"):
            for interface in node.interfaces:
                if not parent or interface not in parent.interfaces:
                    self.add_parent_class(interface, child_class, classes,
                            is_interface=True)
        return classes

    def dump_tree(self, klass, xml_string):
        xml_string += "<item>"
        xml_string += self.format_xref(klass.node, linkname=klass.name)
        for n in klass.children:
            xml_string = self.dump_tree(n, xml_string)
        xml_string += "</item>"
        return xml_string

    def get_leaves(self, klass, leaves):
        if not klass.parents:
            leaves.add(klass)
        for parent in klass.parents:
            self.get_leaves(parent, leaves)
        return leaves

    def dump_class(self, klass):
        leaves = self.get_leaves(klass, leaves=set({}))
        # Needed because the tests are really dumb and use diff :/
        leaves = sorted(leaves, key=lambda leave: getattr(leave, "name"))
        xml_string = "<tree>"
        while leaves:
            klass = leaves.pop()
            xml_string = self.dump_tree(klass, xml_string)
        xml_string += "</tree>"
        return xml_string

    def dump_class_hierarchy(self, node):
        name = "%s.%s" % (node.namespace.name, node.name)
        child_class = HierarchyClass(name, node)
        classes = self.create_hierarchy_classes(node, child_class, dict({}))
        xml_string = self.dump_class(child_class)
        root = ET.fromstring(xml_string)
        indent(root, level=2)
        return ET.tostring(root)

    def format_prerequisites(self, node):
        assert isinstance(node, ast.Interface)

        if len(node.prerequisites) > 0:
            if len(node.prerequisites) > 1:
                return ', '.join(node.prerequisites[:-1]) + \
                    ' and ' + node.prerequisites[-1]
            else:
                return node.prerequisites[0]
        else:
            return 'GObject.Object'

    def format_known_implementations(self, node):
        assert isinstance(node, ast.Interface)

        node_name = node.namespace.name + '.' + node.name
        impl = []

        for c in node.namespace.itervalues():
            if not isinstance(c, ast.Class):
                continue
            for implemented in c.interfaces:
                if implemented.target_giname == node_name:
                    impl.append(c)
                    break

        if len(impl) == 0:
            return 'None'
        else:
            out = '%s is implemented by ' % (node.name,)
            if len(impl) == 1:
                return out + impl[0].name
            else:
                return out + ', '.join(i.name for i in impl[:-1]) + \
                    ' and ' + impl[-1].name


class DocFormatterC(DocFormatter):
    language = "C"
    mime_type = "text/x-csrc"

    fundamentals = {
        "TRUE": "TRUE",
        "FALSE": "FALSE",
        "NULL": "NULL",
    }

    def format_type(self, type_, link=False):
        if isinstance(type_, ast.Array):
            return self.format_type(type_.element_type) + '*'
        elif type_.ctype is not None:
            return type_.ctype
        elif type_.target_fundamental:
            return type_.target_fundamental
        else:
            node = self._transformer.lookup_typenode(type_)
            return getattr(node, 'ctype')

    def format_function_name(self, func):
        if isinstance(func, ast.Function):
            return func.symbol
        else:
            return func.name

    def get_in_parameters(self, node):
        return node.all_parameters


class DocFormatterIntrospectableBase(DocFormatter):
    def should_render_node(self, node):
        if isinstance(node, ast.Record) and node.is_gtype_struct_for is not None:
            return False

        if not getattr(node, "introspectable", True):
            return False

        if isinstance(node, ast.Function) and node.shadowed_by is not None:
            return False

        return super(DocFormatterIntrospectableBase, self).should_render_node(node)


class DocFormatterPython(DocFormatterIntrospectableBase):
    language = "Python"
    mime_type = "text/python"

    fundamentals = {
        "TRUE": "True",
        "FALSE": "False",
        "NULL": "None",
    }

    def should_render_node(self, node):
        if getattr(node, "is_constructor", False):
            return False

        return super(DocFormatterPython, self).should_render_node(node)

    def is_method(self, node):
        if getattr(node, "is_method", False):
            return True

        if isinstance(node, ast.VFunction):
            return True

        return False

    def format_parameter_name(self, node, parameter):
        # Force "self" for the first parameter of a method
        if self.is_method(node) and parameter is node.instance_parameter:
            return "self"
        elif isinstance(parameter.type, ast.Varargs):
            return "..."
        else:
            return parameter.argname

    def format_fundamental_type(self, name):
        fundamental_types = {
            "utf8": "unicode",
            "gunichar": "unicode",
            "gchar": "str",
            "guchar": "str",
            "gboolean": "bool",
            "gint": "int",
            "guint": "int",
            "glong": "int",
            "gulong": "int",
            "gint64": "int",
            "guint64": "int",
            "gfloat": "float",
            "gdouble": "float",
            "gchararray": "str",
            "GParam": "GLib.Param",
            "PyObject": "object",
            "GStrv": "[str]",
            "GVariant": "GLib.Variant"}

        return fundamental_types.get(name, name)

    def format_type(self, type_, link=False):
        if isinstance(type_, (ast.List, ast.Array)):
            return '[' + self.format_type(type_.element_type) + ']'
        elif isinstance(type_, ast.Map):
            return '{%s: %s}' % (self.format_type(type_.key_type),
                                 self.format_type(type_.value_type))
        elif type_.target_giname is not None:
            return type_.target_giname
        else:
            return self.format_fundamental_type(type_.target_fundamental)

    def format_function_name(self, func):
        if func.parent is not None:
            return "%s.%s" % (self.format_page_name(func.parent), func.name)
        else:
            return func.name

    def get_in_parameters(self, node):
        return node.all_parameters


class DocFormatterGjs(DocFormatterIntrospectableBase):
    language = "Gjs"
    mime_type = "text/x-gjs"

    fundamentals = {
        "TRUE": "true",
        "FALSE": "false",
        "NULL": "null",
    }

    def is_method(self, node):
        if getattr(node, "is_method", False):
            return True

        if isinstance(node, ast.VFunction):
            return True

        return False

    def resolve_gboxed_constructor(self, node):
        zero_args_constructor = None
        default_constructor = None

        introspectable_constructors = \
            filter(lambda c: getattr(c, 'introspectable', True),
                   node.constructors)
        for c in introspectable_constructors:
            if zero_args_constructor is None and \
               len(c.parameters) == 0:
                zero_args_constructor = c
            if default_constructor is None and \
               c.name == 'new':
                default_constructor = c
        if default_constructor is None:
            default_constructor = zero_args_constructor
        if default_constructor is None and \
           len(introspectable_constructors) > 0:
            default_constructor = introspectable_constructors[0]

        node.gjs_default_constructor = default_constructor
        node.gjs_zero_args_constructor = zero_args_constructor

    def should_render_node(self, node):
        if isinstance(node, (ast.Compound, ast.Boxed)):
            self.resolve_gboxed_constructor(node)

        if isinstance(node, ast.Compound) and node.disguised and \
           len(node.methods) == len(node.static_methods) == len(node.constructors) == 0:
            return False

        if isinstance(node, ast.ErrorQuarkFunction):
            return False
        if isinstance(node, ast.Field):
            if node.type is None:
                return False
            if isinstance(node.parent, (ast.Class, ast.Union)):
                return False
        if isinstance(node, ast.Union) and node.name is None:
            return False
        if isinstance(node, ast.Class):
            is_gparam_subclass = False
            if node.parent_type:
                parent = self._transformer.lookup_typenode(node.parent_type)
                while parent:
                    if parent.namespace.name == 'GObject' and \
                       parent.name == 'ParamSpec':
                        is_gparam_subclass = True
                        break
                    if parent.parent_type is None:
                        break
                    parent = self._transformer.lookup_typenode(parent.parent_type)
            if is_gparam_subclass:
                return False

        return super(DocFormatterGjs, self).should_render_node(node)

    def format_fundamental_type(self, name):
        fundamental_types = {
            "none": "void",
            "gpointer": "void",
            "gboolean": "Boolean",
            "gint8": "Number(gint8)",
            "guint8": "Number(guint8)",
            "gint16": "Number(gint16)",
            "guint16": "Number(guint16)",
            "gint32": "Number(gint32)",
            "guint32": "Number(guint32)",
            "gchar": "Number(gchar)",
            "guchar": "Number(guchar)",
            "gshort": "Number(gshort)",
            "gint": "Number(gint)",
            "guint": "Number(guint)",
            "gfloat": "Number(gfloat)",
            "gdouble": "Number(gdouble)",
            "utf8": "String",
            "gunichar": "String",
            "filename": "String",
            "GType": "GObject.Type",
            "GVariant": "GLib.Variant",
            # These cannot be fully represented in gjs
            "gsize": "Number(gsize)",
            "gssize": "Number(gssize)",
            "gintptr": "Number(gintptr)",
            "guintptr": "Number(guintptr)",
            "glong": "Number(glong)",
            "gulong": "Number(gulong)",
            "gint64": "Number(gint64)",
            "guint64": "Number(guint64)",
            "long double": "Number(long double)",
            "long long": "Number(long long)",
            "unsigned long long": "Number(unsigned long long)"}

        return fundamental_types.get(name, name)

    def format_type(self, type_, link=False):
        if isinstance(type_, ast.Array) and \
           type_.element_type.target_fundamental in ('gint8', 'guint8'):
            return 'ByteArray'
        elif isinstance(type_, (ast.List, ast.Array)):
            return 'Array(' + self.format_type(type_.element_type, link) + ')'
        elif isinstance(type_, ast.Map):
            return '{%s: %s}' % (self.format_type(type_.key_type, link),
                                 self.format_type(type_.value_type, link))
        elif not type_ or type_.target_fundamental == "none":
            return "void"
        elif type_.target_giname is not None:
            giname = type_.target_giname
            if giname in ('GLib.ByteArray', 'GLib.Bytes'):
                return 'ByteArray'
            if giname == 'GObject.Value':
                return 'Any'
            if giname == 'GObject.Closure':
                return 'Function'
            if link:
                nsname = self._transformer.namespace.name
                if giname.startswith(nsname + '.'):
                    return '<link xref="%s">%s</link>' % (giname, giname)
                else:
                    resolved = self._transformer.lookup_typenode(type_)
                    if resolved:
                        return self.format_xref(resolved)
            return giname
        else:
            return self.format_fundamental_type(type_.target_fundamental)

    def format_function_name(self, func):
        name = func.name
        if func.shadows:
            name = func.shadows

        if func.is_method:
            return "%s.prototype.%s" % (self.format_page_name(func.parent), name)
        elif func.parent is not None:
            return "%s.%s" % (self.format_page_name(func.parent), name)
        else:
            return name

    def format_page_name(self, node):
        if isinstance(node, (ast.Field, ast.Property)):
            return '%s.%s' % (self.format_page_name(node.parent), self.to_underscores(node))
        else:
            return DocFormatterIntrospectableBase.format_page_name(self, node)

    def has_any_parameters(self, node):
        return len(node.parameters) > 0 or \
            node.retval.type.target_fundamental != 'none'

    def get_in_parameters(self, node):
        skip = set()
        for param in node.parameters:
            if param.direction == ast.PARAM_DIRECTION_OUT:
                skip.add(param)
            if param.closure_name is not None:
                skip.add(node.get_parameter(param.closure_name))
            if param.destroy_name is not None:
                skip.add(node.get_parameter(param.destroy_name))
            if isinstance(param.type, ast.Array) and param.type.length_param_name is not None:
                skip.add(node.get_parameter(param.type.length_param_name))

        params = []
        for param in node.parameters:
            if param not in skip:
                params.append(param)
        return params

    def get_out_parameters(self, node):
        skip = set()
        for param in node.parameters:
            if param.direction == ast.PARAM_DIRECTION_IN:
                skip.add(param)
            if param.closure_name is not None:
                skip.add(node.get_parameter(param.closure_name))
            if param.destroy_name is not None:
                skip.add(node.get_parameter(param.destroy_name))
            if isinstance(param.type, ast.Array) and param.type.length_param_name is not None:
                skip.add(node.get_parameter(param.type.length_param_name))

        params = []
        if node.retval.type.target_fundamental != 'none':
            name = 'return_value'
            if node.retval.type.target_fundamental == 'gboolean':
                name = 'ok'

            ret_param = ast.Parameter(name, node.retval.type,
                                      ast.PARAM_DIRECTION_OUT)
            ret_param.doc = node.retval.doc
            params.append(ret_param)
        for param in node.parameters:
            if param not in skip:
                params.append(param)

        if len(params) == 1:
            params[0].argname = 'Returns'

        return params

    def format_in_parameters(self, node):
        in_params = self.get_in_parameters(node)
        return ', '.join(('%s: %s' % (p.argname, self.format_type(p.type, True)))
                         for p in in_params)

    def format_out_parameters(self, node):
        out_params = self.get_out_parameters(node)

        if len(out_params) == 0:
            return 'void'
        elif len(out_params) == 1:
            return self.format_type(out_params[0].type, True)
        else:
            return '[' + ', '.join(('%s: %s' % (p.argname, self.format_type(p.type, True)))
                                   for p in out_params) + ']'

    def field_is_writable(self, node):
        if isinstance(node, ast.Field):
            if node.type is None:
                return False
            if node.private:
                return False
            if isinstance(node.parent, ast.Union):
                return False
            if node.type.target_fundamental not in \
               (None, 'none', 'gpointer', 'utf8', 'filename', 'va_list'):
                return True

            resolved = self._transformer.lookup_typenode(node.type)
            if resolved:
                if isinstance(resolved, ast.Compound) and node.type.ctype[-1] != '*':
                    return self._struct_is_simple(resolved)
                elif isinstance(resolved, (ast.Enum, ast.Bitfield)):
                    return True
            return False
        else:
            return True

    def _struct_is_simple(self, node):
        if node.disguised or len(node.fields) == 0:
            return False

        for f in node.fields:
            if not self.field_is_writable(f):
                return False
        return True

    def format_gboxed_constructor(self, node):
        if node.namespace.name == 'GLib' and node.name == 'Variant':
            return 'signature: String, value: Any'

        zero_args_constructor = node.gjs_zero_args_constructor
        default_constructor = node.gjs_default_constructor

        can_allocate = zero_args_constructor is not None
        if not can_allocate and isinstance(node, ast.Record):
            can_allocate = self._struct_is_simple(node)

        # Small lie: if can_allocate is False, and
        # default_constructor is None, then you cannot
        # construct the boxed in any way. But let's
        # pretend you can with the regular constructor
        if can_allocate or default_constructor is None:
            if isinstance(node, ast.Compound):
                fields = filter(self.field_is_writable, node.fields)
                out = ''
                if len(fields) > 0:
                    out += "{\n"
                    for f in fields:
                        out += "    <link xref='%s.%s-%s'>%s</link>: value\n" % \
                               (node.namespace.name, node.name, f.name, f.name)
                    out += "}"
                return out
            else:
                return ''
        else:
            construct_params = self.get_in_parameters(default_constructor)
            return ', '.join(('%s: %s' % (p.argname, self.format_type(p.type)))
                             for p in construct_params)

LANGUAGES = {
    "c": DocFormatterC,
    "python": DocFormatterPython,
    "gjs": DocFormatterGjs,
}


class DocWriter(object):
    def __init__(self, transformer, language, markdown_include_paths,
            online=False, link_to_gtk_doc=False, resolve_implicit_links=False,
            sections_file=None):
        self._transformer = transformer

        try:
            formatter_class = LANGUAGES[language.lower()]
        except KeyError:
            raise SystemExit("Unsupported language: %s" % (language, ))

        self._formatter = formatter_class(self._transformer,
                markdown_include_paths, online=online,
                link_to_gtk_doc=link_to_gtk_doc,
                resolve_implicit_links=resolve_implicit_links,
                sections_file=sections_file)
        self._language = self._formatter.language

        self._lookup = self._get_template_lookup()

    def _get_template_lookup(self):
        if 'UNINSTALLED_INTROSPECTION_SRCDIR' in os.environ:
            top_srcdir = os.environ['UNINSTALLED_INTROSPECTION_SRCDIR']
            srcdir = os.path.join(top_srcdir, 'giscanner')
        else:
            srcdir = os.path.dirname(__file__)

        template_dir = os.path.join(srcdir, 'doctemplates')

        return TemplateLookup(directories=[template_dir],
                              module_directory=tempfile.mkdtemp(),
                              output_encoding='utf-8')

    def write(self, output):
        try:
            os.makedirs(output)
        except OSError:
            # directory already made
            pass

        self._walk_node(output, self._transformer.namespace, [])
        self._transformer.namespace.walk(lambda node, chain: self._walk_node(output, node, chain))

    def _walk_node(self, output, node, chain):
        if isinstance(node, ast.Function) and node.moved_to is not None:
            return False
        if self._formatter.should_render_node(node):
            self._render_node(node, chain, output)

            # hack: fields are not Nodes in the ast, so we don't
            # see them in the visit. Handle them manually here
            if isinstance(node, (ast.Compound, ast.Class)):
                chain.append(node)
                for f in node.fields:
                    self._walk_node(output, f, chain)
                chain.pop()
            return True
        return False

    def _render_node(self, node, chain, output):
        namespace = self._transformer.namespace

        # A bit of a hack...maybe this should be an official API
        node._chain = list(chain)

        page_kind = get_node_kind(node)
        template_name = '%s/%s.tmpl' % (self._language, page_kind)
        page_id = make_page_id(node)

        template = self._lookup.get_template(template_name)
        result = template.render(namespace=namespace,
                                 node=node,
                                 page_id=page_id,
                                 page_kind=page_kind,
                                 formatter=self._formatter,
                                 ast=ast)

        output_file_name = os.path.join(os.path.abspath(output),
                                        page_id + '.page')
        fp = open(output_file_name, 'w')
        fp.write(result)
        fp.close()
