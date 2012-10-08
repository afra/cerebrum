#Copyright (C) 2012 jaseg <s@jaseg.de>
#
#This program is free software; you can redistribute it and/or
#modify it under the terms of the GNU General Public License
#version 3 as published by the Free Software Foundation.

import subprocess
import os.path
import struct
from mako.template import Template
import binascii
import json
import pylzma
import codecs

autocode_stub = """\
/* AUTOGENERATED CODE FOLLOWS!
 * This file contains the code generated from the module templates as well as
 * some glue logic. It is generated following the device config by "generate.py"
 * in this very folder. Please refrain from modifying it, modify the templates
 * and generation logic instead.
 * 
%if devname:
 * Device name: ${devname},
%endif
 * Build version: ${version}, build date: ${builddate}
 */

#include <string.h>
#include "autocode.h"
#include "comm.h"
"""

autoglue = """
#include "config.h"
#include <avr/pgmspace.h>

comm_callback comm_callbacks[] = {
	% for (callback, id) in callbacks:
	&${callback}, //${id}
	% endfor
};

const uint16_t num_callbacks = ${len(callbacks)};

void init_auto(){
	% for initfunc in init_functions:
		${initfunc}();
	% endfor
}

void loop_auto(){
	% for loopfunc in loop_functions:
		${loopfunc}();
	% endfor
}

void callback_get_descriptor_auto(uint16_t alen, uint8_t* argbuf){
	//FIXME
	uart_putc(auto_config_descriptor_length >> 8);
	uart_putc(auto_config_descriptor_length & 0xFF);
	for(const char* i=auto_config_descriptor; i < auto_config_descriptor+auto_config_descriptor_length; i++){
		uart_putc(pgm_read_byte(i));
	}
	//FIXME add crc generation
	uart_putc(0x00);
	uart_putc(0x00);
}

"""

accessor_callbacks = """
void callback_set_${name}(uint16_t alen, uint8_t* argbuf){
	if(! ${bsize} == alen){
		//FIXME error handling
		return;
	}
	memcpy(&${name}, argbuf, ${bsize});
}

void callback_get_${name}(uint16_t alen, uint8_t* argbuf){
	if(! alen == 0){
		//FIXME error handling
		return;
	}
	uart_putc(${bsize}>>8);
	uart_putc(${bsize}&0xFF);
	for(char* i=((char*)&${name}); i<((char*)&${name})+${bsize}; i++){
		uart_putc(*i);
	}
	//FIXME add crc generation
	uart_putc(0x00);
	uart_putc(0x00);
}

"""

config_c_template = """\
/* AUTOGENERATED CODE AHEAD!
 * This file contains the device configuration in lzma-ed json-format. It is
 * autogenerated by "generate.py" (which should be found in this folder).
 */
#include "config.h"
unsigned int auto_config_descriptor_length = ${desc_len};
const char auto_config_descriptor[] PROGMEM = {${desc}};
"""

def generate(dev, devicename, builddate):
	members = dev["members"]
	seqnum = 23
	current_id = 0
	dev["builddate"] = str(builddate)
	autocode = Template(autocode_stub).render_unicode(devname=devicename, version=dev["version"], builddate=builddate)
	init_functions = []
	loop_functions = []
	
	callbacks = []
	def register_callback(name):
		nonlocal current_id
		callbacks.append((name, current_id))
		old_id = current_id
		current_id += 1
		return old_id

	register_callback("callback_get_descriptor_auto")

	def generate_accessors(name, ctype, aval=1):
		return Template(accessor_callbacks).render_unicode(name=name, bsize="({}*sizeof({}))".format(aval, ctype));

	for mname, member in members.items():
		mfile = member["type"]
		mtype = mfile.replace('-', '_')
		typepath = os.path.join(os.path.dirname(__file__), mfile + ".c.tp")

		def init_function():
			fun = "init_{}_{}".format(mtype, seqnum)
			init_functions.append(fun)
			return fun
		def loop_function():
			fun = "loop_{}_{}".format(mtype, seqnum)
			loop_functions.append(fun)
			return fun

		properties = {}
		functions = {}
		accessors = ""

		def modulevar(name, ctype=None, fmt=None, array=False):
			varname = "modvar_{}_{}_{}".format(mtype, seqnum, name)
			if fmt is not None and ctype is not None:
				nonlocal accessors
				aval = 1
				if array != False:
					aval = array
				accessors += generate_accessors(varname, ctype, aval)

				accid = register_callback("callback_get_" + varname)
				register_callback("callback_set_" + varname)
				properties[name] = {
						"size": struct.calcsize(fmt),
						"id": accid,
						"format": fmt}
				array_component = ""
				if array == True:
					array_component = "[]"
				elif array:
					array_component = "[{}]".format(array)
				return "{} {}{}".format(ctype, varname, array_component)

			return varname

		def module_callback(name, argformat="", retformat=""):
			cbname = 'callback_{}_{}_{}'.format(mtype, seqnum, name)
			cbid = register_callback(cbname)
			func = { 'id': cbid }
			if argformat is not '':
				func['args'] = argformat
			if retformat is not '':
				func['returns'] = retformat
			functions[name] = func
			return cbname

		seqnum += 1
		tp = Template(filename=typepath)
		autocode += tp.render_unicode(
				init_function=init_function,
				loop_function=loop_function,
				modulevar=modulevar,
				module_callback=module_callback,
				member=member)
		autocode += accessors

		if functions:
			member['functions'] = functions
		if properties:
			member['properties'] = properties

	autocode += Template(autoglue).render_unicode(init_functions=init_functions, loop_functions=loop_functions, callbacks=callbacks)
	with open(os.path.join(os.path.dirname(__file__), 'autocode.c'), 'w') as f:
		f.write(autocode)
	config = pylzma.compress(json.JSONEncoder(separators=(',',':')).encode(dev))
	with open(os.path.join(os.path.dirname(__file__), 'config.c'), 'w') as f:
		f.write(Template(config_c_template).render_unicode(desc_len=len(config), desc=','.join(map(str, config))))
	subprocess.call(['/usr/bin/env', 'make', '-C', os.path.dirname(__file__), 'clean', 'all'])
	return dev, os.path.join(os.path.dirname(__file__), 'main.hex')

def commit(args):
	make_env = os.environ.copy()
	make_env["PORT"] = args.port
	subprocess.call(["/usr/bin/env", "make", "-C", os.path.dirname(__file__), "program"], env=make_env)
