# -*- coding: utf-8 -*-
"""
Created on Thu Jul 17 11:35:00 2025

@author: dep07197
"""

import socket
import struct
import platform
import crc32c
import mrtc_pb2
from google.protobuf import json_format

#======================
# MRTC Constants
#======================
trace_pdu = 0           # debug logging of sent and received PDU's
trace_protobuf = 0      # debug logging of sent and received protobuf messages
version_major = mrtc_pb2.CurrentProtocolVersionV2.CURRENT_PROTOCOL_MAJOR_VERSION_NUMBER
version_minor = mrtc_pb2.CurrentProtocolVersionV2.CURRENT_PROTOCOL_MINOR_VERSION_NUMBER
print("MRTC Current version number: " + str(version_major) + "." + str(version_minor))
protocol_version = version_major.to_bytes(4, 'little') + version_minor.to_bytes(4, 'little')
MaxDataLength = 4096  # Restricted value for test purposes, works fine but not needed
MaxDataLength = 2147483647  # maximum value for a signed integer
request_token = 5685        # random number for starting the request tokes which are incremented to make them different

#======================================================
# These are fixed settings for the MRTC communication
#======================================================

# PFLH 1.5T
#remote_ip_address = "192.168.113.107"
#my_ip_address = "192.168.113.108"

# Herzzentrum Leipzig
remote_ip_address = "10.186.47.41"
my_ip_address = "10.186.47.66"

# Demo Best
#remote_ip_address = "130.144.173.74"
#my_ip_address = "130.144.173.79"


exam_card_id_path = "pySuite"
protocol_name1 = "Survey" # Replace this by the Roadmap scan one day
protocol_name2 = "SilentTracking"
init_serv_address = remote_ip_address
init_serv_port = 8174
tc_scan_serv_address = my_ip_address
tc_scan_serv_port = 12345
dicom_server_address = my_ip_address
dicom_server_port = 105
dicom_aetitle = "AESIGNET"
ntp_server_address = my_ip_address
ntp_server_port = 123
active_user = "Gyrotest"
#active_user = "MRUser"


def get_local_time_zone_id():        
     # Expected outcome: "W. Europe Standard Time"   
     
    if platform.system() == 'Linux':
        # This is the required result, so let's hard-wire it for the moment.
        return("W. Europe Standard Time")

    else:    
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
                local_time_zone_id = winreg.QueryValueEx(key, "TimeZoneKeyName")[0]
                print("Platform: Windows, Time zone: " + str(local_time_zone_id))
                return str(local_time_zone_id)
        except OSError:
            pass
        
    return None


# MRTC needs a unique request token, this function returns the next token
def get_request_token():
    global request_token
    request_token += 1
    return request_token.to_bytes(4, byteorder='little')


# Returns a string that contains a description of the PDU
def log_pdu(pdu):
    length = struct.unpack('<I', pdu[:4])[0]
    ver_major, ver_minor = struct.unpack('<II', pdu[4:12])
    message_type = int(struct.unpack('<I', pdu[12:16])[0])
    message_type_str = message_type_to_name(message_type)
    checksum = int(struct.unpack('<I', pdu[length-4:])[0])
    log = f"PDU: {length:4} | {ver_major} | {ver_minor} | {message_type} | {checksum:10} -> {message_type_str:50}"
    return log


# Creates a PDU from a protobuf payload and message type
def pdu_from_payload_and_message_type(payload, message_type):
    length = len(payload) + 20  # 20 = length of other fields (8 + 4 + 4 + 4)
    length_b = length.to_bytes(4, 'little')
    checksum = crc32c.crc32c(length_b + protocol_version + message_type + payload)
    checksum_b = checksum.to_bytes(4, 'little')
    pdu = length_b + protocol_version + message_type + payload + checksum_b
    return pdu


# Returns a protobuf payload and message type from a PDU (and checks whether the checksum is valid)
def payload_and_message_type_from_pdu(pdu):
    length = struct.unpack('<I', pdu[:4])[0]
    message_type = int(struct.unpack('<I', pdu[12:16])[0])
    payload = pdu[16:length - 4]
    checksum = int(struct.unpack('<I', pdu[length-4:])[0])
    checksum2 = crc32c.crc32c(pdu[:length-4])
    if checksum != checksum2:
        print("Checksum incorrect! Found: " + str(checksum) + ", calculated: " + str(checksum2))
    return payload, message_type


# If a fault message is received, it will be logged in this function
def handle_fault_message(payload, pdu_string):
    m0 = mrtc_pb2.FaultResponseMessage()
    m0.ParseFromString(payload)
    print(f"Fault Message received: {m0.error_message}")
    if trace_pdu:
        print(pdu_string)
    if trace_protobuf:
        print(json_format.MessageToJson(m0))


# This function can be used for all request-response messages
# It will handle fault response messages
# Creates a connection and sends protobuf message m1 and receive protobuf response m2 to the address and port
# It will add the request token, create the PDU and unpack the received PDU and add trace logging
# We are the client (using a .connect), scanner is the server
def send_and_receive_message(message_type1, m1, m2, ip_address, port):
    m1.request_token = get_request_token()
    payload = m1.SerializeToString()
    pdu = pdu_from_payload_and_message_type(payload, message_type1)
    with socket.create_connection((ip_address, port)) as s:
        local_ip, local_port = s.getsockname()
        remote_ip, remote_port = s.getpeername()
        if trace_pdu:
            pdu_string = log_pdu(pdu)
            #print(f"{local_ip}:{local_port}  sending " + pdu_string + f" to   {remote_ip}:{remote_port}")
        if trace_protobuf:
            print(json_format.MessageToJson(m1))
        s.sendall(pdu)
        pdu = s.recv(MaxDataLength)
        if trace_pdu:
            pdu_string = log_pdu(pdu)
            #print(f"{local_ip}:{local_port} received " + pdu_string + f" from {remote_ip}:{remote_port}")
    payload, message_type2 = payload_and_message_type_from_pdu(pdu)
    if message_type2 == mrtc_pb2.MessageType.MESSAGE_TYPE_FAULT_RESPONSE:
        handle_fault_message(payload, f"{ip_address}:{port} receiving: ")
    else:
        m2.ParseFromString(payload)
        if trace_protobuf:
            print(json_format.MessageToJson(m2))


# This function can be used for all response-request messages
# It will handle fault response messages
# Once the connection is made on socket s, this function receives protobuf message m1
# and sends protobuf response m2 to the address and port
# It will add the request token as received to the response message and add trace logging
def receive_and_send_message(s, message_type2, m1, m2):
    local_ip, local_port = s.getsockname()
    remote_ip, remote_port = s.getpeername()
    pdu = s.recv(MaxDataLength)
    payload, message_type1 = payload_and_message_type_from_pdu(pdu)
    if message_type1 == mrtc_pb2.MessageType.MESSAGE_TYPE_FAULT_RESPONSE:
        handle_fault_message(payload, f"{local_ip}:{local_port} receiving: ")
    else:
        m1.ParseFromString(payload)
        if trace_pdu:
            pdu_string = log_pdu(pdu)
            print(f"{local_ip}:{local_port} received " + pdu_string + f" from {remote_ip}:{remote_port}")
        if trace_protobuf:
            print(json_format.MessageToJson(m1))
        m2.request_token = m1.request_token
        payload = m2.SerializeToString()
        pdu = pdu_from_payload_and_message_type(payload, message_type2)
        s.sendall(pdu)
        if trace_pdu:
            pdu_string = log_pdu(pdu)
            print(f"{local_ip}:{local_port}  sending " + pdu_string + f" to   {remote_ip}:{remote_port}")
        if trace_protobuf:
            print(json_format.MessageToJson(m2))


# Convenience function to be able to log the message type
def message_type_to_name(val):
    desc = mrtc_pb2.MessageType.DESCRIPTOR
    for (k, v) in desc.values_by_name.items():
        if v.number == val:
            return k
    return None                     # if val isn't a value in MyEnumType


# Starts the connection with scan control, interleaved sending and receiving messages via two sockets s1 and s2
# 1. StartScanRequest (m1 via s1)
# 2. Wait for a connection from the scanner(s2)
# 3. Receive a TakeScanControlRequest and send a response (m2 and m3 via S2)
# 4. Receive StartScanResponse (m4 via s1)
def start_scan_and_take_scan_control_messages(message_type1, message_type3, m1, m2, m3, m4, ip_address, port):
    m1.request_token = get_request_token()
    payload = m1.SerializeToString()
    pdu = pdu_from_payload_and_message_type(payload, message_type1)
    s1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s1.connect((ip_address, port))
    s1.sendall(pdu)
    if trace_pdu:
        local_ip, local_port = s1.getsockname()
        remote_ip, remote_port = s1.getpeername()
        pdu_string = log_pdu(pdu)
        print(f"{local_ip}:{local_port}  sending " + pdu_string + f" to   {remote_ip}:{remote_port}")
    if trace_protobuf:
        print(json_format.MessageToJson(m1))
    # Now we need to listen to scanner and use another socket
    # We are the server (using a .bind), scanner is the client

    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s2.bind((tc_scan_serv_address, tc_scan_serv_port))
    s2.listen()
    print("Listening for incoming connections at: " + str(tc_scan_serv_address) + ":" + str(tc_scan_serv_port))
    s3, client_address = s2.accept()
    s2.close()
    local_ip, local_port = s3.getsockname()
    print(f"Received connection from {client_address} to sock_name {local_ip}:{local_port}")
    # Receive data from the client
    receive_and_send_message(s3, message_type3, m2, m3)
    # Receive the StartScan Response
    pdu2 = s1.recv(MaxDataLength)
    payload, message_type = payload_and_message_type_from_pdu(pdu2)
    if message_type == mrtc_pb2.MessageType.MESSAGE_TYPE_FAULT_RESPONSE:
        handle_fault_message(payload, f"{client_address}:{scan_control_server_port} receiving: ")
    else:
        m4.ParseFromString(payload)
        if trace_pdu:
            local_ip, local_port = s1.getsockname()
            remote_ip, remote_port = s1.getpeername()
            pdu_string = log_pdu(pdu2)
            print(f"{local_ip}:{local_port} received " + pdu_string + f" from {remote_ip}:{remote_port}")
        if trace_protobuf:
            print(json_format.MessageToJson(m4))
        return s3


def pdu_fom_buffer(buffer):
    pdu_length = struct.unpack('<I', buffer[:4])[0]
    buffer_length = len(buffer)
    print(f"buffer length = {buffer_length}, PDU length = {pdu_length}")
    if (pdu_length <= buffer_length):
        return
    else:
        return b'', buffer