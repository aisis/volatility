#!/usr/bin/env python
#
# exportfile.py
# Copyright (C) 2010 Carl Pulley <c.j.pulley@hud.ac.uk>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details. 
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA 
#

"""
This plugin implements the exporting and saving of _FILE_OBJECT's

@author:       Carl Pulley
@license:      GNU General Public License 2.0 or later
@contact:      c.j.pulley@hud.ac.uk
@organization: University of Huddersfield
"""

import re
import commands as exe
import getpass
import os
import os.path
import math
import platform
import struct
import sys
import hashlib
import datetime
import volatility.utils as utils
import volatility.obj as obj
import volatility.win32.tasks as tasks
import volatility.debug as debug
import volatility.commands as commands
import volatility.plugins.filescan as filescan
try:
	import magic
except:
	print "python-magic is not installed, see https://github.com/ahupp/python-magic for further information"

class ExportException(Exception):
	"""General exception for handling warnings and errors during exporting of files"""
	pass

class ExportFile(filescan.FileScan):
	"""
	Given a PID, _EPROCESS or _FILE_OBJECT, extract the associated (or given) 
	_FILE_OBJECT's from memory.
	
	All exported files are written to a DFXML file. This XML file may then be 
	processed using various AFFLib compatible tools (e.g. PyFlag).
	
	Should python-magic (see https://github.com/ahupp/python-magic) be 
	installed, then the DFXML output will contain libmagic tags for each file 
	page.
	
	This plugin is particularly useful when one expects memory to be fragmented, 
	and so, linear carving techniques (e.g. using scalpel or foremost) might be 
	expected to fail. See reference [5] (below) for more information regarding 
	some of the forensic benefits of accessing files via the _FILE_OBJECT data 
	structure.
	
	REFERENCES:
	[1] Russinovich, M., Solomon, D.A. & Ionescu, A., 2009. Windows Internals: 
	    Including Windows Server 2008 and Windows Vista, Fifth Edition (Pro 
	    Developer) 5th ed. Microsoft Press.
	[2] Information on mapping _FILE_OBJECT's to _EPROCESS (accessed 6/Oct/2011):
	    http://computer.forensikblog.de/en/2009/04/linking_file_objects_to_processes.html
	[3] OSR Online article: Finding File Contents in Memory (accessed 7/Oct/2011):
	    http://www.osronline.com/article.cfm?article=280
	[4] Extracting Event Logs or Other Memory Mapped Files from Memory Dumps by 
	    J. McCash (accessed 7/Oct/2011):
	    http://computer-forensics.sans.org/blog/2011/02/01/digital-forensics-extracting-event-logs-memory-mapped-files-memory-dumps
	[5] Physical Memory Forensics for Files and Cache by J. Butler and J. 
	    Murdock (accessed 8/Oct/2011):
	    https://media.blackhat.com/bh-us-11/Butler/BH_US_11_ButlerMurdock_Physical_Memory_Forensics-WP.pdf
	[6] CodeMachine article on Prototype PTEs (accessed 8/Oct/2011):
	    http://www.codemachine.com/article_protopte.html
	[7] OSR Online article: Cache Me If You Can: Using the NT Chace Manager (accessed 14/Oct/2011):
	    http://www.osronline.com/article.cfm?id=167
	"""

	meta_info = dict(
		author = 'Carl Pulley',
		copyright = 'Copyright (c) 2010 Carl Pulley',
		contact = 'c.j.pulley@hud.ac.uk',
		license = 'GNU General Public License 2.0 or later',
		url = 'http://helios.hud.ac.uk/scomcjp',
		os = ['WinXPSP2x86', 'WinXPSP3x86'],
		version = '1.0',
		)

	def __init__(self, config, *args):
		filescan.FileScan.__init__(self, config, *args)
		config.add_option("pid", type = 'int', action = 'store', help = "Extract all associated _FILE_OBJECT's from a PID")
		config.add_option("eproc", type = 'int', action = 'store', help = "Extract all associated _FILE_OBJECT's from an _EPROCESS offset (kernel address)")
		config.add_option("fobj", type = 'int', action = 'store', help = "Extract a given _FILE_OBJECT offset (physical address)")
		config.add_option("pool", default = False, action = 'store_true', help = "Extract all _FILE_OBJECT's found by searching the pool")

	def calculate(self):
		self.kernel_address_space = utils.load_as(self._config)
		self.flat_address_space = utils.load_as(self._config, astype = 'physical')
		if not(bool(self._config.pid) ^ bool(self._config.eproc) ^ bool(self._config.fobj) ^ bool(self._config.pool)):
			if not(bool(self._config.pid) or bool(self._config.eproc) or bool(self._config.fobj) or bool(self._config.pool)):
				debug.error("exactly *ONE* of the options --pid, --eproc, --fobj or --pool must be specified (you have not specified _any_ of these options)")
			else:
				debug.error("exactly *ONE* of the options --pid, --eproc, --fobj or --pool must be specified (you have used _multiple_ such options)")
		if bool(self._config.pid):
			# --pid
			eproc_matches = [ eproc for eproc in tasks.pslist(self.kernel_address_space) if eproc.UniqueProcessId == self._config.pid ]
			if len(eproc_matches) != 1:
				debug.error("--pid needs to take a *VALID* PID argument (could not find PID {0} in the process listing for this memory image)".format(self._config.pid))
			return self.dump_from_eproc(eproc_matches[0])
		elif bool(self._config.eproc):
			# --eproc
			return self.dump_from_eproc(obj.Object("_EPROCESS", offset = self._config.eproc, vm = self.kernel_address_space))
		elif bool(self._config.fobj):
			# --fobj
			try:
				file_object = obj.Object("_FILE_OBJECT", offset = self._config.fobj, vm = self.flat_address_space)
				return filter(None, [ self.dump_file_object(file_object) ])
			except ExportException as exn:
				debug.error(exn)
		else:
			# --pool
			return self.dump_from_pool()

	def dump_from_eproc(self, eproc):
		result = []
		if eproc.ObjectTable.HandleTableList:
			for h in eproc.ObjectTable.handles():
				h.kas = self.kernel_address_space
				if h.get_object_type() == "File":
					file_obj = obj.Object("_FILE_OBJECT", h.Body.obj_offset, h.obj_vm)
					try:
						result += [ self.dump_file_object(file_obj) ]
					except ExportException as exn:
						debug.warning(exn)
		return filter(None, result)

	def dump_from_pool(self):
		result = []
		for object_obj, file_obj, name in filescan.FileScan.calculate(self):
			try:
				result += [ self.dump_file_object(file_obj) ]
			except ExportException as exn:
				debug.warning(exn)
		return filter(None, result)

	# TODO: add in timestamp etc. information to fileobjects
	def render_text(self, outfd, file_data):
		outfd.write("<?xml version='1.0' encoding='UTF-8'?>\n")
		outfd.write("<dfxml xmloutputversion=\"0.3\">\n")
		outfd.write("  <creator>\n")
		outfd.write("    <program>Volatility Framework: exportfile plugin</program>\n")
		outfd.write("    <version>{0}</version>\n".format(self.meta_info['version']))
		outfd.write("    <execution_environment>\n")
		sysname, nodename, release, version, machine, processor = platform.uname()
		outfd.write("      <os_sysname>{0}</os_sysname>\n".format(sysname))
		outfd.write("      <os_release>{0}</os_release>\n".format(release))
		outfd.write("      <os_version>{0}</os_version>\n".format(version))
		outfd.write("      <host>{0}</host>\n".format(nodename))
		outfd.write("      <arch>{0}</arch>\n".format(processor))
		if sys.argv:
			outfd.write("      <command_line>{0}/bin/python {1}</command_line>\n".format(unicode(sys.prefix), unicode(" ".join(sys.argv))))
		outfd.write("      <uid>{0}</uid>\n".format(os.getuid()))
		outfd.write("      <username>{0}</username>\n".format(getpass.getuser()))
		outfd.write("      <start_date>{0:%Y-%m-%dT%H:%M:%SZ}</start_date>\n".format(datetime.datetime.utcnow()))
		outfd.write("    </execution_environment>\n")
		outfd.write("  </creator>\n")
		outfd.write("  <source>\n")
		outfd.write("    <image_filename>{0}</image_filename>\n".format(unicode(self.flat_address_space.name)))
		outfd.write("  </source>\n")
		for file_obj in sorted(file_data, key=lambda x: x['object']):
			outfd.write("  <fileobject offset=\"{0:d}\" section=\"{1:d}\">\n".format(file_obj['object'], file_obj['section']))
			if 'name' in file_obj:
				outfd.write("    <filename>{0}</filename>\n".format(unicode(file_obj['name'])))
			if 'size' in file_obj:
				outfd.write("    <filesize>{0:d}</filesize>\n".format(file_obj['size']))
			sector_sizes = set([ int(p['sectors']) for p in file_obj['pages'] if 'sectors' in p ])
			if len(sector_sizes) == 1:
				outfd.write("    <sectorsize>512</sectorsize>\n")
				outfd.write("    <sectors>{0:d}</sectors>\n".format(list(sector_sizes)[0]))
			first_pages = [ p for p in file_obj['pages'] if p['start_addr'] == 0 ]
			if len(first_pages) == 1 and 'libmagic' in first_pages[0]:
				outfd.write("    <libmagic>{0}</libmagic>\n".format(first_pages[0]['libmagic']))
			outfd.write("    <byte_runs>\n")
			for page in sorted(file_obj['pages'], key=lambda x: x['start_addr']):
				outfd.write("      <byte_run offset=\"{0:d}\" img_offset=\"{1:d}\" len=\"{2:d}\">\n".format(page['start_addr'], page['offset'], page['size']))
				if 'libmagic' in page:
					outfd.write("    <libmagic>{0}</libmagic>\n".format(page['libmagic']))
				for hash_type, hash_value in page['hash'].items():
					outfd.write("        <hexdigest type=\"{0}\">{1}</hexdigest>\n".format(hash_type, hash_value))
				outfd.write("      </byte_run>\n")
			outfd.write("    </byte_runs>\n")
			outfd.write("  </fileobject>\n")
		outfd.write("</dfxml>\n")

	KB = 0x400
	_1KB = KB
	MB = _1KB**2
	_1MB = MB
	GB = _1KB**3
	_1GB = GB

	# TODO: monitor issue 151 on Volatility trunk (following code is copied from FileScan)
	def parse_string(self, unicode_obj):
		"""Unicode string parser"""
		# We need to do this because the unicode_obj buffer is in
		# kernel_address_space
		string_length = unicode_obj.Length
		string_offset = unicode_obj.Buffer
        
		string = self.kernel_address_space.read(string_offset, string_length)
		if not string:
			return ''
		return string[:260].decode("utf16", "ignore").encode("utf8", "xmlcharrefreplace")

	def read_large_integer(self, large_integer):
		return large_integer.HighPart.v() * pow(2, 4 *8) + large_integer.LowPart.v()

	def walk_vacb_tree(self, depth, ptr):
		if depth < 1:
			raise ExportException("consistency check failed (expected VACB tree to have a positive depth)")
		if depth == 1:
			return [ (obj.Object("_VACB", offset = ptr + 4*index, vm = self.kernel_address_space), index) for index in range(0, 128) ]
		return [ (vacb, 128*index + section) for index in range(0, 128) for (vacb, section) in self.walk_vacb_tree(depth-1, ptr+4*index) ]

	def read_vacbs_from_cache_map(self, shared_cache_map, file_size, depth = None, ptr = None):
		if file_size < self._1MB:
			return [ (shared_cache_map.InitialVacbs[index], index) for index in range(0, 4) ]
		else:
			tree_depth = math.ceil((math.ceil(math.log(file_size, 2)) - 18)/7)
			return self.walk_vacb_tree(tree_depth, shared_cache_map.Vacbs.v())

	# TODO: add in volume information to returned dictionaries (e.g. from DeviceObject or Vpb?)
	# TODO: factor in RelatedFileObject
	def dump_file_object(self, file_object):
		if not file_object.is_valid():
			raise ExportException("consistency check failed (expected [_FILE_OBJECT @ 0x{0:08X}] to be a valid physical address)".format(file_object.v()))
		file_name = self.parse_string(file_object.FileName)
		if file_name == None:
			debug.warning("expected file name to be non-null [_FILE_OBJECT @ 0x{0:08X}]".format(file_object.v()))
		section_object_ptr = obj.Object('_SECTION_OBJECT_POINTERS', offset = file_object.SectionObjectPointer, vm = self.kernel_address_space)
		result = None
		if section_object_ptr.DataSectionObject != 0 and section_object_ptr.DataSectionObject != None:
			# Shared Cache Map
			try:
				shared_cache_map = obj.Object('_SHARED_CACHE_MAP', offset = section_object_ptr.SharedCacheMap, vm = self.kernel_address_space)
				if shared_cache_map == None:
					raise ExportException("consistency check failed [_FILE_OBJECT @ 0x{0:08X}] (expected _SHARED_CACHE_MAP to be non-null for file name '{1}')".format(file_object.v(), file_name))
				if shared_cache_map.FileSize == None:
					raise ExportException("consistency check failed [_FILE_OBJECT @ 0x{0:08X}] (expected FileSize to be non-null for file name '{1}')".format(file_object.v(), file_name))
				file_size = self.read_large_integer(shared_cache_map.FileSize)
				if self.read_large_integer(shared_cache_map.ValidDataLength) > file_size:
					raise ExportException("consistency check failed [_FILE_OBJECT @ 0x{0:08X}] (expected ValidDataLength to be bounded by file size for file name '{1}')".format(file_object.v(), file_name))
				result = { 'object': file_object.v(), 'section': section_object_ptr.v(), 'size': file_size, 'pages': self.dump_shared_cache_map(shared_cache_map, file_size) }
				if file_name:
					result['name'] = file_name
			except ExportException as exn:
				debug.warning(exn)
		if section_object_ptr.DataSectionObject != 0 and section_object_ptr.DataSectionObject != None:
			# Data Section Object
			try:
				control_area = obj.Object("_CONTROL_AREA", offset = section_object_ptr.DataSectionObject, vm = self.kernel_address_space)
				result = { 'object': file_object.v(), 'section': section_object_ptr.v(), 'pages': self.dump_control_area(control_area) }
				if file_name:
					result['name'] = file_name
			except ExportException as exn:
				debug.warning(exn)
		if section_object_ptr.ImageSectionObject != 0 and section_object_ptr.ImageSectionObject != None:
			# Image Section Object
			try:
				control_area = obj.Object("_CONTROL_AREA", offset = section_object_ptr.ImageSectionObject, vm = self.kernel_address_space)
				result = { 'object': file_object.v(), 'section': section_object_ptr.v(), 'pages': self.dump_control_area(control_area) }
				if file_name:
					result['name'] = file_name
			except ExportException as exn:
				debug.warning(exn)
		return result

	def walk_subsections(self, ptr):
		if ptr == 0 or ptr == None:
			return []
		return [ ptr ] + self.walk_subsections(ptr.NextSubsection)

	# TODO: take into account the PTE flags when reading pages (output via debug.warning?)
	def read_pte_array(self, subsection, base_addr, num_of_ptes):
		result = []
		for pte in range(0, num_of_ptes):
			if not(self.kernel_address_space.is_valid_address(base_addr + 4*pte)):
				raise ExportException("consistency check failed (expected PTE to be at a valid address)")
			(pte_addr, ) = struct.unpack('=I', self.kernel_address_space.read(base_addr + 4*pte, 4))
			page_phy_addr = pte_addr & 0xFFFFF000
			if page_phy_addr != 0 and self.flat_address_space.is_valid_address(page_phy_addr) and page_phy_addr != subsection.v():
				page = self.flat_address_space.read(page_phy_addr, 4*self.KB)
				page_md5 = hashlib.md5(page).hexdigest()
				page_sha1 = hashlib.sha1(page).hexdigest()
				try:
					libmagic = magic.from_buffer(page)
				except:
					libmagic = None # We assume that python-magic is not installed and ignore any errors
				result += [ ({ 'md5': page_md5, 'sha1': page_sha1 }, libmagic, page_phy_addr, pte) ]
			elif page_phy_addr != 0:
				result += [ (None, None, None, pte) ]
		return result

	def dump_shared_cache_map(self, shared_cache_map, file_size):
		# _VACB points to a 256KB area of paged memory. Parts of this memory may be paged out, so we perform a linear search, 
		# through our set of _VACB page addresses, looking for valid in-memory pages.
		result = []
		for vacb, section in self.read_vacbs_from_cache_map(shared_cache_map, file_size):
			if vacb != 0 and vacb != None:
				vacb_base_addr = vacb.BaseAddress.v()
				for page in range(0, 64):
					if self.kernel_address_space.is_valid_address(vacb_base_addr + page*(4*self.KB)):
						page_data = self.kernel_address_space.read(vacb_base_addr + page*(4*self.KB), (4*self.KB))
						page_md5 = hashlib.md5(page_data).hexdigest()
						page_sha1 = hashlib.sha1(page_data).hexdigest()
						result_page = { 'hash': { 'md5': page_md5, 'sha1': page_sha1 }, 'offset': self.kernel_address_space.vtop(vacb_base_addr + page*(4*self.KB)), 'start_addr': section*(256*self.KB) + page*(4*self.KB), 'size': 4*self.KB }
						try:
							result_page['libmagic'] = magic.from_buffer(page_data)
						except:
							pass # We assume that python-magic is not installed and ignore any errors
						if result_page not in result:
							result += [ result_page ]
		return result

	def dump_control_area(self, control_area):
		result = []
		start_subsection = obj.Object("_SUBSECTION", offset = control_area.v() + control_area.size(), vm = self.kernel_address_space)
		subsection_list = self.walk_subsections(start_subsection)
		sector = None
		for subsection, index in zip(subsection_list, range(0, len(subsection_list))):
			start_sector = subsection.StartingSector
			num_of_sectors = subsection.NumberOfFullSectors
			if sector == None:
				sector = start_sector
			sector_data = [ (page, libmagic, page_phy_addr, pte_index) for page, libmagic, page_phy_addr, pte_index in self.read_pte_array(subsection, subsection.SubsectionBase.v(), subsection.PtesInSubsection) if pte_index <= num_of_sectors and page != None ]
			result_pages = map(lambda ((page, libmagic, page_phy_addr, pte_index), position): ({ 'hash': page, 'libmagic': libmagic, 'sectors': subsection.NumberOfFullSectors, 'offset': page_phy_addr, 'start_addr': (sector + position*8)*512, 'size': 4*self.KB } if libmagic else { 'hash': page, 'sectors': subsection.NumberOfFullSectors, 'offset': page_phy_addr, 'start_addr': (sector + position*8)*512, 'size': 4*self.KB }), zip(sector_data, range(0, len(sector_data))))
			for result_page in result_pages:
				if result_page not in result:
					result += [ result_page ]
			sector += len(sector_data)*8
		return result
