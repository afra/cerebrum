#Copyright (C) 2012 jaseg <s@jaseg.de>
#
#This program is free software; you can redistribute it and/or
#modify it under the terms of the GNU General Public License
#version 3 as published by the Free Software Foundation.

import subprocess
import os.path
import time
from threading import Thread
import struct
from mako.template import Template
import binascii
import json
try:
    import lzma
except:
    import pylzma as lzma
import codecs
import unittest
"""Automatic Cerebrum c code generator"""

# Code templates. Actually, this is the only device-dependent place in this whole
# file, and actually only a very few lines are device-dependent.
# FIXME: Break this stuff into a common "C code generator" which is included from
# here and from the msp code generator and which is feeded with the two or three
# device-dependent lines

autocode_header = """\
/* AUTOGENERATED CODE FOLLOWS!
 * This file contains the code generated from the module templates as well as
 * some glue logic. It is generated following the device config by "generate.py"
 * in this very folder. Please refrain from modifying it, modify the templates
 * and generation logic instead.
 * 
 * Build version: ${version}, build date: ${builddate}
 */

#include <string.h>
#include "autocode.h"
#include "comm.h"
#include "uart.h"
"""

#This one contains the actual callback/init/loop magick.
autocode_footer = """
#include "config.h"
#if defined(__AVR__)
#include <avr/pgmspace.h>
#endif

const comm_callback comm_callbacks[] = {
    % for (callback, id) in callbacks:
    &${callback}, //${id}
    % endfor
};

const uint16_t callback_count = (sizeof(comm_callbacks)/sizeof(comm_callback)); //${len(callbacks)};

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

void callback_get_descriptor_auto(uint16_t payload_offset, uint16_t alen, uint8_t* argbuf){
    //FIXME
    uart_putc(auto_config_descriptor_length >> 8);
    uart_putc(auto_config_descriptor_length & 0xFF);
    for(const char* i=auto_config_descriptor; i < auto_config_descriptor+auto_config_descriptor_length; i++){
#if defined(__AVR__)
        uart_putc(pgm_read_byte(i));
#else
        uart_putc(*i);
#endif
    }
    //FIXME add crc generation
    uart_putc(0x00);
    uart_putc(0x00);
}

"""

# Template for the accessor callbacks generated for each parameter
accessor_callbacks = """
void callback_set_${name}(uint16_t payload_offset, uint16_t alen, uint8_t* argbuf){
    //FIXME add some error handling here or there?
    memcpy(((char*)&${name})+payload_offset, argbuf, alen);
    //if(payload_offset+alen >= ${bsize}){ //end of buffer reached
        ${set_action}
        //response code length
        uart_putc(0x00);
        uart_putc(0x00);
        //not-yet-crc
        uart_putc(0x00);
        uart_putc(0x00);
    //}
}

void callback_get_${name}(uint16_t payload_offset, uint16_t alen, uint8_t* argbuf){
    if(alen != 0){
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
#ifndef PROGMEM
#define PROGMEM
#endif

unsigned int auto_config_descriptor_length = ${desc_len};
const char auto_config_descriptor[] PROGMEM = {${desc}};
"""

#FIXME possibly make a class out of this one
def generate(desc, device, build_path, builddate, target = 'all'):
    members = desc["members"]
    seqnum = 23 #module number (only used during build time to generate unique names)
    current_id = 0
    desc["builddate"] = str(builddate)
    autocode = Template(autocode_header).render_unicode(version=desc["version"], builddate=builddate)
    init_functions = []
    loop_functions = []
    callbacks = []

    def register_callback(name):
        nonlocal current_id
        callbacks.append((name, current_id))
        old_id = current_id
        current_id += 1
        return old_id

    #Default callback number 0
    register_callback("callback_get_descriptor_auto")

    def generate_accessors(name, ctype, aval=1, set_action=""):
        return Template(accessor_callbacks).render_unicode(name=name, bsize="({}*sizeof({}))".format(aval, ctype), set_action=set_action);

    for mname, member in members.items():
        mfile = member["type"]
        mtype = mfile.replace('-', '_')
        typepath = os.path.join(build_path, mfile + ".c.tp")

        #CAUTION! These *will* exhibit strange behavior when called more than once!
        def init_function():
            fun = "init_{}_{}".format(mtype, seqnum)
            init_functions.append(fun)
            return fun
        def loop_function():
            fun = "loop_{}_{}".format(mtype, seqnum)
            loop_functions.append(fun)
            return fun

        #module instance build config entries
        properties = {}
        functions = {}
        #accessor method c source code <== will be inserted at the end of autocode.c
        accessors = ""

        #FIXME possibly swap the positions of ctype and fmt
        def modulevar(name, ctype=None, fmt=None, array=False, access="rw", set_action=""):
            """Get the c name of a module variable and possibly register the variable with the code generator.

                If only name is given, the autogenerated c name of the module variable will be returned.
                
                If you provide fmt, accessor methods for the variable will be registered (but not *generated*!)
                and the variable will be registered as a property in the build config (using the previously
                mentioned accessor methods). If you also provide ctype the accessors will also be generated.
                array can be used to generated accessors for module variables that are arrays.

                access can be used independent with at lest fmt given to specify the access type of the new module
                parameter. The string will be copied to the build config 1:1 though this generator currently only
                differentiates between "rw" and "r".
                
            """
            varname = "modvar_{}_{}_{}".format(mtype, seqnum, name)
            if fmt is not None:
                nonlocal accessors
                aval = 1
                if array != False:
                    aval = array

                accid = register_callback("callback_get_" + varname)
                if "w" in access:
                    register_callback("callback_set_" + varname)

                properties[name] = {
                        "size": struct.calcsize(fmt),
                        "id": accid,
                        "fmt": fmt}
                if access is not "rw":
                    #Save some space in the build config (that later gets burned into the µC's really small flash!)
                    properties[name]["access"] = access

                if ctype is not None:
                    #NOTE: Even if the parameter is marked "read-only", a setter will be generated. I currently
                    #am too lazy to fix that since it does no harm because the linker will just throw it away since it
                    #is not registered and thus not used anywhere.
                    accessors += generate_accessors(varname, ctype, aval, set_action)
                    array_component = ""
                    if array == True:
                        array_component = "[]"
                    elif array:
                        array_component = "[{}]".format(array)
                    return "{} {}{}".format(ctype, varname, array_component)
            else:
                assert(ctype is None)

            return varname

        def module_callback(name, argformat="", retformat=""):
            """Register a regular module callback.
            
                I hereby officially discourage the (sole) use of this function since these callbacks or functions as they
                appear at the Cerebrum level cannot be automatically mapped to snmp MIBs in any sensible manner. Thus, please
                use accessors for everything if possible, even if it is stuff that you would not traditionally use them for.
                For an example on how to generate and register custom accessor methods please see simple-io.c.tp .

            """
            cbname = 'callback_{}_{}_{}'.format(mtype, seqnum, name)
            cbid = register_callback(cbname)
            func = { 'id': cbid }
            #Save some space in the build config (that later gets burned into the µC's really small flash!)
            if argformat is not '':
                func['args'] = argformat
            if retformat is not '':
                func['returns'] = retformat
            functions[name] = func
            return cbname

        #Flesh out the module template!
        tp = Template(filename=typepath)
        autocode += tp.render_unicode(
                init_function=init_function,
                loop_function=loop_function,
                modulevar=modulevar,
                module_callback=module_callback,
                member=member,
                device=device)
        autocode += accessors

        #Save some space in the build config (that later gets burned into the µC's really small flash!)
        if functions:
            member['functions'] = functions
        if properties:
            member['properties'] = properties

        #increment the module number
        seqnum += 1

    #finish the code generation and write the generated code to a file
    autocode += Template(autocode_footer).render_unicode(init_functions=init_functions, loop_functions=loop_functions, callbacks=callbacks)
    with open(os.path.join(build_path, 'autocode.c'), 'w') as f:
        f.write(autocode)
    #compress the build config and write it out
    #config = lzma.compress(bytes(json.JSONEncoder(separators=(',',':')).encode(desc), 'ASCII'))
    config = bytes(json.JSONEncoder(separators=(',',':')).encode(desc), 'ASCII')
    with open(os.path.join(build_path, 'config.c'), 'w') as f:
        f.write(Template(config_c_template).render_unicode(desc_len=len(config), desc=','.join(map(str, config))))
    #compile the whole stuff
    make_env = os.environ.copy()
    make_env['MCU'] = device.get('mcu')
    subprocess.call(['/usr/bin/env', 'make', '--no-print-directory', '-C', build_path, 'clean', target], env=make_env)

    return desc

def commit(device, build_path, args):
    """Flash the newly generated firmware onto the device"""
    make_env = os.environ.copy()
    make_env['MCU'] = device.get('mcu')
    make_env['PORT'] = args.port
    make_env['PROGRAMMER'] = device.get('programmer')
    make_env['BAUDRATE'] = str(device.get('baudrate'))
    subprocess.call(['/usr/bin/env', "make",'--no-print-directory',  '-C', build_path, 'program'], env=make_env)

class TestBuild(unittest.TestCase):

    def setUp(self):
        pass

    def test_basic_build(self):
        generate({'members': {}, 'version': 0.17}, {'mcu': 'test'}, 'test', '2012-11-14 20:11:01')

class TestCommStuff(unittest.TestCase):
    
    def setUp(self):
        generate({'members': {}, 'version': 0.17}, {'mcu': 'test'}, 'test', '2012-11-14 20:11:01', 'test')
        self.terminated = False

    def new_test_process(self):
        #spawn a new communication test process
        p = subprocess.Popen([os.path.join(os.path.dirname(__file__), 'common', 'comm-test')], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        #start a thread killing that process after a few seconds
        def kill_subprocess():
            time.sleep(5)
            if ( not p.returncode or p.returncode < 0 ) and not self.terminated:
                p.terminate()
                self.assert_(False, 'Communication test process terminated due to a timeout')

        t = Thread(target=lambda: kill_subprocess())
        t.daemon = True
        t.start()
        return (p, p.stdin, p.stdout, t)

    def test_config_descriptor(self):
        (p, stdin, stdout, t) = self.new_test_process();

        stdin.write(b'\\#\x00\x00\x00\x00')
        stdin.flush()
        stdin.close()

        (length,) = struct.unpack('>H', stdout.read(2))
        self.assertEqual(length, 75, 'Incorrect config descriptor length')
        data = stdout.read(length)
        stdout.read(2) #read and ignore the not-yet-crc
        self.assertEqual(data, b']\x00\x00\x80\x00\x00=\x88\x8a\xc6\x94S\x90\x86\xa6c}%:\xbbAj\x14L\xd9\x1a\xae\x93n\r\x10\x83E1\xba]j\xdeG\xb1\xba\xa6[:\xa2\xb9\x8eR~#\xb9\x84%\xa0#q\x87\x17[\xd6\xcdA)J{\xab*\xf7\x96%\xff\xfa\x12g\x00', 'wrong config descriptor returned')
        
        p.terminate()
        self.terminated = True

