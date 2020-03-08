#!/usr/bin/env python3
'''
    Based on info from http://cm2020.sourceforge.net/

    Simple logging tool for battery charger Voltcraft Charge Manager CM2020 into InfluxDB.
    Assumes local DB called "cm2020"

    Requires installed python influxdb client lib.
'''

import logging
import logging.handlers
import argparse
import threading
import serial
import time
import struct

from influxdb import InfluxDBClient
from datetime import datetime



running = True
buffer = bytearray(0)
max_buff_size = 1000
buff_lock = threading.Lock()

#---------------------- Read serial thread ----------------------
def read_serial(device):
    global running
    global buffer
    global max_buff_size
    global buff_lock

    device.flush()
    while running:
        n = device.in_waiting
        if n > 50:
            buff_lock.acquire()
            buffer.extend(device.read(n))
            buff_lock.release()
            logging.debug("Read another {} bytes, now got {}".format(n, len(buffer)))
        time.sleep(0.1)

        if len(buffer) > max_buff_size:
            logging.error("Buffer overflow, ending")
            running = False
            break

    logging.info("Ending read thread")

#---------------------- Parse single slot ----------------------
program_enum = { 0x7: "Charge",
                 0x8: "Discharge",
                 0x9: "Check",
                 0xa: "Cycle",
                 0xb: "Alive"
                 }

last_voltage    = []
last_current    = []

def process_slot(slot, buf):
    global last_voltage
    global last_ch_voltage
    global last_current

    logging.debug("Process slot {}".format(slot))
    (slot, c1, prg, stg, ccap1, ccap2, dcap1, dcap2, volt, curr, hour, minute, switch, act, aux1, maxcurr, counter) = struct.unpack(">BHBBBHBHHHBBBBBBB", buf)

    ccap = (ccap1 * 0x10000 + ccap2) / 100.0
    dcap = (dcap1 * 0x10000 + dcap2) / 100.0
    inst_voltage = volt / 1000.0 # mV -> V
    inst_current = curr / 1000.0 # mA -> A
    max_curr = maxcurr / 10.0 # max charging current in mA -> A

    # charging program like cycle, alive, ...   
    program = program_enum.get(prg & 0x0f)

    # status - combination of stage, active, status
    status_idx = (prg & 0xf0) / 0x10

    status = ""
    if status_idx in [4,6] or stg==8:
        status = "Finished"
    elif status_idx == 8:
        status = "Error"
    elif stg in [1, 3, 5]:
        status = "Charging"
    elif stg in [2, 4, 6]:
        status = "Discharging"
    elif stg == 7:
        status = "Trickle"
    elif not program:
        status = "---"
    else:
        status = "<{} {}>".format(status_idx, stg) # fallback for debug, should not happen

    if not program:
        program = "---"
        inst_voltage = 0.0

    # only valid when non zero switch or when finished
    if switch==0 and (prg & 0xf0)==0:
        voltage                 = last_voltage[slot-1]
    else:
        last_voltage[slot-1] = inst_voltage
        voltage              = inst_voltage

    # always valid when charging and only when non zero switch when discharging
    if switch==0 and stg!=1 and stg!=3 and stg!=5:
        current = last_current[slot-1]
    else:
        last_current[slot-1] = inst_current
        current              = inst_current

    print("S{:02} {:11}/{:9} ccap={:9.2f}mAh dcap={:9.2f}mAh voltage={:6.3f}V current={:6.3f}A maxcurr={}A chtime={:02d}:{:02d}".format(slot,
                                                                                                                                        status,
                                                                                                                                        program,
                                                                                                                                        ccap,
                                                                                                                                        dcap,
                                                                                                                                        voltage,
                                                                                                                                        current,
                                                                                                                                        max_curr,
                                                                                                                                        hour,
                                                                                                                                        minute))

    data = {
        "measurement": "CM2020",
        "time": datetime.utcnow().isoformat(),
        "tags": {
            "slot": "S{:02d}".format(slot)
        },
        "fields": {
            "status": status,
            "program": program,
            "ccap": ccap,
            "dcap": dcap,
            "voltage": voltage,
            "current": current,
            "mac_curr": max_curr,
            "duration": "{:02d}:{:02d}".format(hour, minute)
        }
    }
    return data


#---------------------- Main data process loop ----------------------
def proces_data(test_only):
    global buffer
    global buff_lock

    # --- wait for enough data for sync ---
    while len(buffer) < 440:
        logging.debug("Wait for data")
        time.sleep(0.5)

    # --- synchronize ---
    offset = 0
    found = False
    
    buff_lock.acquire()
    for offset in range(220):
        found = True
        for slot in range(10):
            if buffer[offset+slot*22] != slot+1:
                found = False
                break
        
        if found:
            logging.debug("Found sync at offset {}".format(offset))
            break

    if not found:
        logging.error("Cannot synchronize...")
        return

    buffer = buffer[offset:]
    buff_lock.release()

    client = None
    if not test_only:
        client = InfluxDBClient(database='cm2020')

    # --- start processing ---
    while True:
        while len(buffer) < 220:
            logging.debug("Wait for data")
            time.sleep(0.5)

        buff_lock.acquire()
        local_buffer = buffer[:220]
        buffer = buffer[220:]
        buff_lock.release()

        json_data = []
        for slot in range(10):
            data = process_slot(slot+1, local_buffer[slot*22:(slot+1)*22])
            json_data.append(data)

        if not test_only:
            if not client.write_points(json_data):
                logging.error("Influx problem")

        print("------------------")


#--------------------------- MAIN ---------------------------
def main():
    global running
    global last_voltage
    global last_current

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s'
                        )
    
    rootLogger = logging.getLogger()

    logging.addLevelName( logging.INFO, "\033[93m%s\033[1;0m" % logging.getLevelName(logging.INFO))
    logging.addLevelName( logging.WARNING, "\033[1;31m%s\033[1;0m" % logging.getLevelName(logging.WARNING))
    logging.addLevelName( logging.ERROR, "\033[1;41m%s\033[1;0m" % logging.getLevelName(logging.ERROR))

    parser = argparse.ArgumentParser(description='Process some integers.')

    parser.add_argument("-s",
                        "--serial",
                        dest    = "device",
                        default = "/dev/ttyUSB0",
                        help    = "Serial device. Default is /dev/ttyUSB0")

    parser.add_argument("-v",
                        "--verbose",
                        action  = "store_true",
                        dest    = "debugOn",
                        default = False,
                        help    = "Verbose")

    parser.add_argument("-t",
                        "--test",
                        action  = "store_true",
                        dest    = "testOnly",
                        default = False,
                        help    = "Test only, do no sotre to the DB")


    options = parser.parse_args()

    if(options.debugOn):
        rootLogger.setLevel(logging.DEBUG)        
        logging.info("Verbose ON")


    for a in range(10):
        last_voltage.append(0.0)
        last_current.append(0.0)

    serial_port = serial.Serial(options.device, 9600, timeout=0) # non blocking reads
    thread = threading.Thread(target=read_serial, args=(serial_port,))
    thread.start()

    proces_data(options.testOnly)
    logging.info("Process ended")
    running = False

#------------------------------------------------------------
if __name__ == "__main__":
    main()
