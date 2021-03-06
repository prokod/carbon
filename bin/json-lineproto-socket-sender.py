#!/usr/bin/env python

"""This will read a file containing newline-separated strings, each of
which is a json document containing a list of one or more metrics.

It'll turn the result of evaluating the line/document into individal
items that will be sent it to the destination as line protocol carbon
metrics.

Usage:
json-lineproto-socket-sender.py <host> <port> <filename>

This will write instrumentation data back to fd 0 in the form of:
<number of metrics>,<number of bytes>,<time taken to send>

XXX it would be useful to gracefully handle signals, e.g. being able
to get a TERM, and so return stats at the time of the TERM.  Currently
if we take too long, we'll just get killed by the queue-runner

"""

import os
import sys
import struct
import cPickle as pickle
import time
import socket
import json

def line_format(metric_list):
    """For some weird reason, the line protocol is data then date.  The internal
    representation is date then data.  Oh, well..."""
    return "\n".join([ "{0[0]}  {0[1][1]}  {0[1][0]}".format(m) for m in metric_list]) + "\n"

def main():
    fname   = sys.argv[3]
    f       = open(fname)
    f.seek(-1, 2)
    size    = f.tell()
    f.seek(0)
    timeout = 60

    if size == 0:
        os.unlink(fname)
        sys.exit(1) # 1 will mean no data

    try:
        conn = socket.create_connection((sys.argv[1], sys.argv[2],), timeout)
    except Exception as e:
        print "ERROR: Trying to connect to the remote: {0}:{1}".format(sys.argv[1], sys.argv[2])
        print "ERROR: message is {0}".format(str(e))
        print "ERROR: exiting."
        sys.exit(100)

    start_time   = time.time()
    metric_count = 0
    errored      = False

    for line in f:
        try:
            l = json.loads(line)
            p = line_format(l)
            metric_count += len(l)
        except TypeError as te:
            print "ERROR: TypeError trying to pickle '{0}'".format(line)
            errored = True
            continue
        except ValueError as ve:
            print "ValueError: there is a problematic line in {0}".format(line)
            print "ValueError: the message is {0}".format(str(ve))

        try:
            conn.sendall(p)
        except Exception as another_error:
            print "ERROR: Some other error trying to send {0}".format(fname)
            print "ERROR: the message is: {0}".format(str(another_error))
            print "ERROR: exiting."
            sys.exit(100)

    end_time           = time.time()
    time_taken         = end_time - start_time
    bytes_per_second   = float(size) / float(time_taken)
    metrics_per_second = float(metric_count) / float(time_taken)

    # if called from the command line, this may fail.  If called from
    # queue-runner.py, this should succeed.
    try:
        os.write(0, "{0:.2f},{1:.2f},{2:.6f}".format(
            float(metric_count), float(size), float(time_taken)))
    except OSError:
        print("INFO: {0} sent {1} bytes for {2} metrics in {3} second(s) ({4} bytes/second, {5} metrics/second) from {6}".format(
            os.path.basename(sys.argv[0]), size, metric_count, time_taken, bytes_per_second, metrics_per_second, fname))

    os.unlink(sys.argv[3])

if __name__ == '__main__':
    main()
