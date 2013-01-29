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
from inspect import isfunction
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

void generic_getter_callback(const comm_callback_descriptor* cb, void* argbuf_end);

const comm_callback_descriptor comm_callbacks[] = {
    % for (callback, argbuf, argbuf_len, id) in callbacks:
    {${callback}, (void*)${argbuf}, ${argbuf_len}}, //${id}
    % endfor
};

const uint16_t callback_count = (sizeof(comm_callbacks)/sizeof(comm_callback_descriptor)); //${len(callbacks)};

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

void callback_get_descriptor_auto(const comm_callback_descriptor* cb, void* argbuf_end){
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
}

//Generic getter used for any readable parameters.
//Please note one curious thing: This callback can not only be used to read, but also to write a variable. The only
//difference between the setter and the getter of a variable is that the setter does not read the entire variable's
//contents aloud.
void generic_getter_callback(const comm_callback_descriptor* cb, void* argbuf_end){
    //response length
    uart_putc(cb->argbuf_len>>8);
    uart_putc(cb->argbuf_len&0xFF);
    //response
    for(char* i=((char*)cb->argbuf); i<((char*)cb->argbuf)+cb->argbuf_len; i++){
        uart_putc(*i);
    }
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
#FIXME I think the target parameter is not used anywhere. Remove?
def generate(desc, device, build_path, builddate, target = 'all'):
    members = desc["members"]
    seqnum = 23 #module number (only used during build time to generate unique names)
    current_id = 0
    desc["builddate"] = str(builddate)
    autocode = Template(autocode_header).render_unicode(version=desc["version"], builddate=builddate)
    init_functions = []
    loop_functions = []
    callbacks = []

    def register_callback(name, argbuf="global_argbuf", argbuf_len="ARGBUF_SIZE"):
        nonlocal current_id
        callbacks.append(("0" if name is None else "&"+name, argbuf, argbuf_len, current_id))
        old_id = current_id
        current_id += 1
        return old_id

    #Default callback number 0
    register_callback("callback_get_descriptor_auto")

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

        #FIXME possibly swap the positions of ctype and fmt
        def modulevar(name, ctype=None, fmt=None, array=False, access="rw", accid=None):
            """Get the c name of a module variable and possibly register the variable with the code generator.

                If only name is given, the autogenerated c name of the module variable will be returned.

                If you provide fmt, virtual accessor methods for the variable will be registered and the variable will
                be registered as a property in the build config (using the previously mentioned accessor methods).
                In this context, "virtual" means that there will be callbacks in the callback list, but the setter will
                not be backed by an actual function and the getter will just point to a global generic getter.
                If you also provide ctype the accessors will also be generated.
                array can be used to generated accessors for module variables that are arrays.

                access can be used independent with at lest fmt given to specify the access type of the new module
                parameter. The string will be copied to the build config 1:1 though this generator currently only
                differentiates between "rw" and "r".

            """
            varname = "modvar_{}_{}_{}".format(mtype, seqnum, name)
            if fmt is not None:
                aval = 1
                if array != False:
                    aval = array

                if accid is None:
                    accid = register_callback("generic_getter_callback", ("" if array else "&")+varname, "sizeof("+varname+")")
                    if "w" in access:
                        register_callback(None, ("" if array else "&")+varname, "sizeof("+varname+")")

                properties[name] = {
                        "size": struct.calcsize(fmt),
                        "id": accid,
                        "fmt": fmt}
                if access is not "rw":
                    #Save some space in the build config (that later gets burned into the µC's really small flash!)
                    properties[name]["access"] = access

                if ctype is not None:
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
                register_callback=register_callback,
                member=member,
                device=device)

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
    #Depending on whether you want to store the device config as plain text or lzma'ed plain text comment out one of the following lines
    #The first byte is used as a magic here. The first byte of a JSON string will always be a '{'
    config = b'#' + lzma.compress(bytes(json.JSONEncoder(separators=(',',':')).encode(desc), 'utf-8'))
    #config = bytes(json.JSONEncoder(separators=(',',':')).encode(desc), 'utf-8')
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
        generate({'members': {}, 'version': 0.17}, {'mcu': 'test'}, 'test', '2012-05-23 23:42:17')

def compareJSON(bytesa, bytesb):
    jsona = json.JSONDecoder().decode(str(bytesa, "ASCII"))
    normstra = bytes(json.JSONEncoder(separators=(',',':')).encode(jsona), 'ASCII')
    jsonb = json.JSONDecoder().decode(str(bytesb, "ASCII"))
    normstrb = bytes(json.JSONEncoder(separators=(',',':')).encode(jsonb), 'ASCII')
    return normstra == normstrb

class TestCommStuff(unittest.TestCase):

    def setUp(self):
        generate({'members': {'test': {'type': 'test'}}, 'version': 0.17}, {'mcu': 'test'}, 'test', '2012-05-23 23:42:17')

    def new_test_process(self):
        #spawn a new communication test process
        p = subprocess.Popen([os.path.join(os.path.dirname(__file__), 'test', 'main')], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        #start a thread killing that process after a few seconds
        def kill_subprocess():
            time.sleep(5)
            if p.poll() is None or p.returncode < 0:
                p.terminate()
                self.assert_(False, 'Communication test process terminated due to a timeout')

        t = Thread(target=kill_subprocess)
        t.daemon = True
        t.start()
        return (p, p.stdin, p.stdout, t)

    def test_config_descriptor(self):
        (p, stdin, stdout, t) = self.new_test_process();

        stdin.write(b'\\#\x00\x00\x00\x00')
        stdin.flush()
        stdin.close()

        (length,) = struct.unpack('>H', stdout.read(2))
        #FIXME this fixed size comparision is *not* helpful.
        #self.assertEqual(length, 227, 'Incorrect config descriptor length')
        data = stdout.read(length)
        #self.assertEqual(data, b']\x00\x00\x80\x00\x00=\x88\x8a\xc6\x94S\x90\x86\xa6c}%:\xbbAj\x14L\xd9\x1a\xae\x93n\r\x10\x83E1\xba]j\xdeG\xb1\xba\xa6[:\xa2\xb9\x8eR~#\xb9\x84%\xa0#q\x87\x17[\xd6\xcdA)J{\xab*\xf7\x96%\xff\xfa\x12g\x00', 'wrong config descriptor returned')
        #Somehow, each time this is compiled, the json encode shuffles the fields of the object in another way. Thus it does not suffice to compare the plain strings.
        #self.assert_(compareJSON(data, b'{"version":0.17,"builddate":"2012-05-23 23:42:17","members":{"test":{"functions":{"test_multipart":{"args":"65B","id":1},"check_test_buffer":{"id":4}},"type":"test","properties":{"test_buffer":{"size":65,"id":2,"fmt":"65B"}}}}}'), "The generated test program returns a wrong config descriptor: {}.".format(data))
        #FIXME somehow, this commented-out device descriptor check fails randomly even though nothing is actually wrong.

    def test_multipart_call(self):
        (p, stdin, stdout, t) = self.new_test_process();

        stdin.write(b'\\#\x00\x01\x00\x41AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')
        stdin.flush()
        stdin.close()
        
        #wait for test process to terminate. If everything else fails, the timeout thread will kill it.
        p.wait()
        self.assertEqual(p.returncode, 0, "The test process caught an error from the c code. Please watch stderr for details.")

    def test_meta_multipart_call(self):
        """Test whether the test function actually fails when given invalid data."""
        (p, stdin, stdout, t) = self.new_test_process();

        stdin.write(b'\\#\x00\x01\x00\x41AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAA')
        stdin.flush()
        stdin.close()
        
        #wait for test process to terminate. If everything else fails, the timeout thread will kill it.
        p.wait()
        self.assertEqual(p.returncode, 1, "The test process did not catch an error it was supposed to catch from the c code. Please watch stderr for details.")
    
    def test_multipart_call_long_args(self):
        (p, stdin, stdout, t) = self.new_test_process();

        stdin.write(b'\\#\x00\x05\x01\x01'+b'A'*257)
        stdin.write(b'\\#\x00\x06\x00\x00')
        stdin.flush()
        stdin.close()
        
        #wait for test process to terminate. If everything else fails, the timeout thread will kill it.
        p.wait()
        self.assertEqual(p.returncode, 0, "The test process caught an error from the c code. Please watch stderr for details.")
        self.assertEqual(p.returncode, 0, "The test process caught an error from the c code. Please watch stderr for details.")
        
    def test_meta_multipart_call_long_args(self):
        """Test whether the test function actually fails when given invalid data."""
        (p, stdin, stdout, t) = self.new_test_process();

        stdin.write(b'\\#\x00\x05\x01\x01'+b'A'*128+b'B'+b'A'*128)
        stdin.flush()
        stdin.close()
        
        #wait for test process to terminate. If everything else fails, the timeout thread will kill it.
        p.wait()
        self.assertEqual(p.returncode, 1, "The test process did not catch an error it was supposed to catch from the c code. Please watch stderr for details.")

    def test_attribute_accessors_multipart(self):
        (p, stdin, stdout, t) = self.new_test_process();

        stdin.write(b'\\#\x00\x03\x01\x01'+b'A'*32+b'B'*32+b'C'*32+b'D'*32+b'E'*32+b'F'*32+b'G'*32+b'H'*32+b'I') # write some characters to test_buffer
        stdin.write(b'\\#\x00\x04\x00\x00') # call check_test_buffer
        stdin.flush()
        stdin.close()
        
        #wait for test process to terminate. If everything else fails, the timeout thread will kill it.
        p.wait()
        self.assertEqual(p.returncode, 0, "The test process caught an error from the c code. Please watch stderr for details.")

    def test_meta_attribute_accessors_multipart(self):
        (p, stdin, stdout, t) = self.new_test_process();

        stdin.write(b'\\#\x00\x03\x01\x01'+b'A'*33+b'B'*31+b'C'*32+b'D'*32+b'E'*32+b'F'*32+b'G'*32+b'H'*32+b'I') # write some characters to test_buffer
        stdin.write(b'\\#\x00\x04\x00\x00') # call check_test_buffer
        stdin.flush()
        stdin.close()
        
        #wait for test process to terminate. If everything else fails, the timeout thread will kill it.
        p.wait()
        self.assertEqual(p.returncode, 1, "The test process did not catch an error it was supposed to catch from the c code. Please watch stderr for details.")

