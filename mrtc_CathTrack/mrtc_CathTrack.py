#####################################################################################################################
#  Example script for active catheter tracking via the MRTC interface                                               #
#   1. Synch system configuration                                                                                   #
#   2. Get Hospital ExamCards                                                                                       #
#   3. Synch active user                                                                                            #
#   4. Start Exam Request                                                                                           #
#   5. Receive "images" (1D projections for each channel)                                                           #
#   6. Build a dictionary for each tracking "shot", including XYZ coil coordinates and transformation matrices      #
#      for 3D slicer                                                                                                #
#   7. Send transformation matrix to slicer                                                                         #
#                                                                                                                   #
#   Authors: Jouke Smink, Philips MR Clinical Science (MRTC communication)                                          #
#            Christian Stehning, Philips MR Clinical Science (data handling and coil signal localisation, partly    #
#            based on code written by Sascha Krueger and Steffen Weiss, Philips Research Lab Hamburg)               #
#            Ingo Paetsch, Leipzig Heart Centre (communication with 3D slicer)                                      #
#            Leonard Euler (simplified calculation of transformation matrices)                                      #
#####################################################################################################################

from time import time, sleep, process_time
from datetime import datetime
import numpy as np
import matplotlib.pyplot as plt
import struct
import copy
import pickle

# ===============================================================================
# This is for creating, converting  and searching mutable, nested dictionaries
# ===============================================================================
from collections import defaultdict, Counter
from typing import Any
from nested_lookup import nested_lookup

# =====================================================================
# This is to run the MRTC interface
# =====================================================================
import mrtc_pb2
import mrtc_func
from google.protobuf import json_format

# ====================================================================
# This is for transformation matrices and communication with Slicer
# ====================================================================
import pyigtl

# ====================================================================
# This is for communication with HoloLens
# =====================================================================
import socket

# ========================================================================================================================================
# These lists map the channel numbers onto (human-readable) identifiers. Also assign signal colors to the channels (for plotting)
# In my notes of 2020, the channel numbers on the interface box
# are these:
#
# Anschluss der Katheterleitungen an die Interface-Box: die Eingänge sind von links nach rechts nummeriert. Dabei kommt an
#
# 1 – Abl distal
# 2 – Abl proximal
# 3 – Diag distal
# 4 – Diag proximal
# ========================================================================================================================================
chMap = (
    ("Abl_01", "dist"),  # ch0
    ("Abl_01", "prox"),  # ch1
    ("Ref_01", "dist"),  # ch2
    ("Ref_01", "prox"))  # ch3


# ==================================================================
# This maps the catheter names onto HoloLens catheter IDs
# ==================================================================
cathID = {"Abl_01": 1, "Ref_01": 0}

# ==================================================================
# This maps the catheter names onto the port numbers for the
# communication with HoloLens
# ==================================================================
cathPort = {"Abl_01": 5006, "Ref_01": 5005}

# ==============================================================================================================
# Make sets for the available catheters and coils. This is useful in various parts of the scripts (e.g. loops)
# ==============================================================================================================
caths = set([cath[0] for cath in chMap])
coils = set([coil[1] for coil in chMap])

# =================================================================
# This maps the slice numbers onto (human-readable) orientations
# =================================================================
oriMap = (
    "tra",  # slice 0
    "sag",  # slice 1
    "cor")  # slice 2

# =================================================================================================================================================================================
# Entries from xml-configuation for Vision MR catheters (perhaps we should just parse the xml-File)
# <DualCoilCatheter MaxExtent="5.0" Name="Vision MR" DeviceHardware="DualCoilCatheter" Diameter="3" DistanceBetweenCoils="5.8" DistanceBetweenTipAndNearestCoil="8.1"/>
# =================================================================================================================================================================================
noiseThreshold = 200  # Threshold above which we assume that this is a real signal
minSNRtoTrustPosition = 10  # Minimum required SNR to trust a peak position 
distanceBetweenCoils = 9  # distance between coil elements in mm (was 7.77)
distanceBetweenTipAndNearestCoil = 8.1  # distance between the distal coil and the catheter tip
margin = 4.0  # allowed tolerance of distance between coils (was 2.0)
maxExtent = 2.0  # Maximum width of a peak (sensitivity profile of microcoil)
maxRelPeakDiff = 20  # Maximum relative difference between peaks to accept them as a peak pair left and right of the actual coil
maxAveragingDistance = 20  # If the catheter is moved very quickly, averaging may result in the catheter apparantly taking an "illegal shortcut" (direct path between positions). Weight samples less that are far away from the current position.


# ======================================================================================
# Want positions to be averaged for smoothness? Keep a list of N last positions, then.
# ======================================================================================
numAverages = 5

# ================================================
# Want transformation matrices sent to Slicer?
# ================================================
sendToSlicer = True

# =================================================
# Want coil positions sent to HoloLens?
# =================================================
sendToHoloLens = False
udp_ip = "127.0.0.1"  # Currently localhost


# ==================================================================================
# Replay data from pickle file? If empty, normal MRTC communcation will be started.
# ==================================================================================
replayPickleFile = ""
#replayPickleFile = r"/home/christian-stehning/MRDATA/2026_06_07_jerking_caths/tracking_data_2026-06-04_14-58-27.pkl"


# ================================================
# Configuration for MRTC
# ================================================
debug_tracking = False
debug_mrtc = False
database_version = "00000000-00-00"


# =======================================================================
# This function allows to create a mutable dictionary (of dictionaries)
# It does not require any initialisation
# I don't understand this recursive syntax either, but it works nicely.
# =======================================================================
def makehash():
    return defaultdict(makehash)


# ====================================================================================
# Storing, reading and browsing a "defaultdict" can be tricky.
# Therefore, we convert the (nested) default dictionaries into regular dictionaries
# prior to storing the data
# ====================================================================================
def defaultdict_to_dict(d: Any) -> Any:
    """
    Recursively convert a defaultdict (possibly nested) to a regular dict.
    Works for any depth and preserves non-dict values.
    """
    if isinstance(d, defaultdict):
        # Convert each value recursively
        return {k: defaultdict_to_dict(v) for k, v in d.items()}
    elif isinstance(d, dict):
        # Handle normal dicts that may contain defaultdicts inside
        return {k: defaultdict_to_dict(v) for k, v in d.items()}
    else:
        # Base case: not a dict, return as is
        return d

# ===================================================================================================================
# We need to keep a few global counters and dictionaries.
# Will be used in the function that is called when a fresh shot is received. The shotList will be saved at the end.
# ===================================================================================================================
ord_number = -1
last_dyn = 1
last_slice_number = 0

# =================================================================================
# (Mutlable) dictionaries that hold the received tracking data
# =================================================================================
curMutableDict = makehash() # A mutable, nested dictionary, will be filled on the fly as the data come in
shotList = []  # A list of dictionaries that holds the data for each tracking shot

# ===========================================================================================================
# Function to detect the position of the peak in one projection. Similar algorithm as in the iSuite SW.
# Reason behind the name is that depending on the readout orientation wrt the shaft, we may see two peaks
# (signal "left and right" of the catheter shaft), or one peak (readout along shaft direction)
# ===========================================================================================================
def findDualOrSinglePeak(rawData, voxelSizeM):
    # We may have one large or two peaks on both sides of the receive coil. Get the position of the two largest peaks in the raw data
    peakIdxs = np.argpartition(rawData.flatten(), -2)[-2:]  # Index 1 holds the larger of the two peaks
    peakPos = peakIdxs[1] * voxelSizeM
    dualPeak = False

    # Are these two peaks within a reasonable distance (max extent, on both sides of the catheter shaft)?
    # Are they also similar in height?
    if (voxelSizeM * (peakIdxs[1] - peakIdxs[0]) < maxExtent) and ((rawData[peakIdxs[1]] - rawData[peakIdxs[0]]) / rawData[peakIdxs[1]]< (maxRelPeakDiff / 100)):
        # If so, locate the catheter between the peaks using a center of mass approach
        peakPos = np.average(peakIdxs, 0, rawData[peakIdxs].flatten()) * voxelSizeM
        dualPeak = True

    peakVal = rawData[peakIdxs[1]]
    return (peakPos, peakVal, peakIdxs[1], dualPeak)


# ===================================================================================================
# Images are received as messages and do not require a response, so simple receive
# image_m = image message
# m1 = mrtc_pb2.ReleaseScanControlRequestMessage()
# m2 = mrtc_pb2.ReleaseScanControlResponseMessage()
# m3 = mrtc_pb2.SetStackPositionsRequestMessage()
# m4 = mrtc_pb2.SetStackPositionsResponseMessage()
# num_images_to_set_stack_pos = the number of images to wait for seting the new stack positions
# ==================================================================================================
def receive_message(s, image_m, m1, m2, m3, m4, num_images_to_set_stack_pos):
    images = []
    scan_names = []
    plt.ion()
    buffer = b""
    do_recv = True
    images_ctr = 0
    # Process a complete PDU
    while True:
        try:
            # Receive network data
            while True:
                if do_recv:
                    if debug_mrtc:
                        print("\nwaiting on .recv for data")
                    data = s.recv(mrtc_func.MaxDataLength)
                    buffer += data
                    if debug_mrtc:
                        print(f"data received: {len(data)}")
                else:
                    if debug_mrtc:
                        print("\n~~~~~~~~~~~~~~ data already in buffer ~~~~~~~~~~~~~~")
                pdu_length = struct.unpack("<I", buffer[:4])[0]
                message_type = int(struct.unpack("<I", buffer[12:16])[0])
                message_type_str = mrtc_func.message_type_to_name(message_type)
                if debug_mrtc:
                    print(f"PDU: {pdu_length:4} | {message_type} -> {message_type_str}")
                buffer_length = len(buffer)
                if debug_mrtc:
                    print(f"Buffer length: {buffer_length} PDU length: {pdu_length}")
                if buffer_length >= pdu_length:
                    if debug_mrtc:
                        print("Buffer length >= pdu_length")
                    pdu = buffer[:pdu_length]
                    buffer = buffer[pdu_length:]
                    # buffer should be empty now, but if there is still a FULL PDU in there, then skip the next .recv
                    buffer_length = len(buffer)
                    if buffer_length >= 4:
                        pdu_length = struct.unpack("<I", buffer[:4])[0]
                        if buffer_length >= pdu_length:
                            do_recv = False
                        else:
                            do_recv = True
                    else:
                        do_recv = True
                    break
                else:
                    if debug_mrtc:
                        print("~~~~~~~~~~~~~~ Buffer length < pdu_length ~~~~~~~~~~~~~~")
                    do_recv = True
            if debug_mrtc:
                print("Determine payload")
            payload, message_type = mrtc_func.payload_and_message_type_from_pdu(pdu)

            if mrtc_func.trace_pdu:
                local_ip, local_port = s.getsockname()
                remote_ip, remote_port = s.getpeername()
                pdu_string = mrtc_func.log_pdu(pdu)
                if debug_mrtc:
                    print(f"{local_ip}:{local_port} received "+ pdu_string+ f" from {remote_ip}:{remote_port}")
            
            if message_type == mrtc_pb2.MessageType.MESSAGE_TYPE_IMAGE_DATA:
                # Process the image message and print the scan token
                image_m.ParseFromString(payload)
                
                if debug_mrtc:
                    print("Image scan token: "+ hex(int.from_bytes(image_m.scan_token, byteorder="big")).upper())
                
                #Send data to this function for evaluation and forwarding to 3DSlicer
                tracking_data_to_slicer(image_m, images, scan_names, shotList)
                
                images_ctr += 1
                if images_ctr >= num_images_to_set_stack_pos:
                    num_images_to_set_stack_pos = 2147483647  # maximum value for a signed integer                    

                    # Create a SetStackPositionRequest message (m3)
                    m3.request_token = mrtc_func.get_request_token()
                    m3.scan_token = image_m.scan_token
                    stack_position = m3.stack_positions.add()
                    stack_position.stack_number = 1
                    stack_position.center_point_mm.x = 11.11
                    stack_position.center_point_mm.y = 22.22
                    stack_position.center_point_mm.z = 33.33
                    stack_position.slice_orientation.row_direction_cosines.x = 1
                    stack_position.slice_orientation.row_direction_cosines.y = 0
                    stack_position.slice_orientation.row_direction_cosines.z = 0
                    stack_position.slice_orientation.column_direction_cosines.x = 0
                    stack_position.slice_orientation.column_direction_cosines.y = -1
                    stack_position.slice_orientation.column_direction_cosines.z = 0
                    payload = m3.SerializeToString()
                    message_type3 = struct.pack("<I",mrtc_pb2.MessageType.MESSAGE_TYPE_SET_STACK_POSITIONS_REQUEST)
                    pdu3 = mrtc_func.pdu_from_payload_and_message_type(payload, message_type3)
                    s.sendall(pdu3)
                    if mrtc_func.trace_pdu:
                        local_ip, local_port = s.getsockname()
                        remote_ip, remote_port = s.getpeername()
                        pdu_string = mrtc_func.log_pdu(pdu3)
                        if debug_mrtc:
                            print(f"\n{local_ip}:{local_port} sending " + pdu_string + f" to {remote_ip}:{remote_port}")
                    if mrtc_func.trace_protobuf:
                        print(json_format.MessageToJson(m3))
            elif (message_type == mrtc_pb2.MessageType.MESSAGE_TYPE_SET_STACK_POSITIONS_RESPONSE):
                m4.ParseFromString(payload)
                if debug_mrtc:
                    print("Set stack positions response message received!")
                if mrtc_func.trace_protobuf:
                    print(json_format.MessageToJson(m4))
                if debug_mrtc:
                    print(f"Success: {m4.stack_positions_status == mrtc_pb2.StackPositionsStatus.STACK_POSITIONS_STATUS_ACTIVE}")
            elif (message_type == mrtc_pb2.MessageType.MESSAGE_TYPE_RELEASE_SCAN_CONTROL_REQUEST):
                m1.ParseFromString(payload)
                if mrtc_func.trace_protobuf:
                    print(json_format.MessageToJson(m1))
                # Create a ReleaseScanControlResponse message (m2)
                m2.request_token = m1.request_token
                payload = m2.SerializeToString()
                message_type2 = struct.pack("<I",mrtc_pb2.MessageType.MESSAGE_TYPE_RELEASE_SCAN_CONTROL_RESPONSE)
                pdu2 = mrtc_func.pdu_from_payload_and_message_type(payload, message_type2)
                mrtc_func.s.sendall(pdu2)
                if mrtc_func.trace_pdu:
                    local_ip, local_port = s.getsockname()
                    remote_ip, remote_port = s.getpeername()
                    pdu_string = mrtc_func.log_pdu(pdu2)
                    print(f"{local_ip}:{local_port} sending " + pdu_string + f" to {remote_ip}:{remote_port}")
                if mrtc_func.trace_protobuf:
                    print(json_format.MessageToJson(m2))
                print("\nEnd of the image data stream. A release scan control request message has been received")
                break
            else:
                if debug_mrtc:
                    print("Error: no image and no release scan message")
        except ConnectionResetError:
            print("Connection closed by the remote side")
            break
    print("Closing the connection")
    s.close()


# ==================================================================================
# Function to send the catheter coil positions to the HoloLens via UDP
# ==================================================================================
def send_catheter(timestamp, catheter_id, proximal_xyz, distal_xyz, sock, udp_ip, udp_port):
    """
    two strings for the two catheters are sent to two different ports via UDP
    timestap: sequence number of package
    catheter_id: 0 (green) or 1 (red)
    proximal_xyz: [x, y, z] coordinates of proximal coil (coil at lower end of tip)
    distal_xyz: [x, y, z] coordinate of distal coil (coil at upper end of tip)
    """
    message = f"{int(timestamp)},{catheter_id},{proximal_xyz[0]},{proximal_xyz[1]},{proximal_xyz[2]},{distal_xyz[0]},{distal_xyz[1]},{distal_xyz[2]}".encode("utf-8")
    sock.sendto(message, (udp_ip, udp_port))



# ===============================================================================
# Average position over the last N valid shots. Weight averages inverse to their
# distance to the current position. This avoids that if a catheter is moved quickly,
# or the last known, position is far away for other reasons, avaraging would hallucinate
# an artificial catheter trajectory to the current position.
#=========================================================================================
def smart_averaging_weighted_mean(cath, shot, shotList, numAverages, maxAveragingDistance):
    if shot[cath]["valid"]:

        avgCoilPositionXYZ = {}
        for coil in coils:
            # Init (list)
            posVector = [shot[cath][coil]["coilPositionXYZ"]]
            distVector = [0.0]
            n = 1
            while (n < len(shotList)) and len(posVector)<numAverages:
                if shotList[-n][cath]["valid"]:
                    posVector.append(shotList[-n][cath][coil]["coilPositionXYZ"])
                    distVector.append(posVector[-1] - posVector[0])
                n = n + 1

            # Offset to avoid zeros in weight denominator
            weightsVector = [1/(np.linalg.norm(distVector[i]) + maxAveragingDistance) for i in range(len(distVector))]

            avgCoilPositionXYZ[coil] = np.average(posVector, weights=weightsVector, axis=0)

        avgCenterPos = (avgCoilPositionXYZ["dist"] + avgCoilPositionXYZ["prox"]) / 2
        avgDirVector = avgCoilPositionXYZ["dist"] - avgCoilPositionXYZ["prox"]
        coilDistance = np.linalg.norm(avgDirVector)
        if coilDistance != 0:
            avgDirVector /= coilDistance

    return (avgCenterPos, avgDirVector)


# ====================================================================================================
# Convert the catheter tip position and orientation into a transformation matrix that warps a model
# of the catheter onto the corresponding position in 3Dslicer
#=====================================================================================================
def pos_to_matrix(pos, dirVector):
    # ===========================================================================================================================================
    # References (rotation matrix)
    # https://math.stackexchange.com/questions/1956699/getting-a-transformation-matrix-from-a-normal-vector
    # Leonhard Euler, "Problema algebraicum ob affectiones prorsus singulares memorabile", Commentatio 407 Indicis Enestoemiani, Novi Comm. Acad. Sci. Petropolitanae 15 (1770), 75–106
    # ===========================================================================================================================================

    matrix = np.eye(4)

    nx, ny, nz = dirVector[0], dirVector[1], dirVector[2]
    nxy_len = np.sqrt(nx * nx + ny * ny)

    # Rotation matrix
    matrix[0, 0] = ny / nxy_len
    matrix[1, 0] = -nx / nxy_len
    matrix[2, 0] = 0

    matrix[0, 1] = nx * nz / nxy_len
    matrix[1, 1] = ny * nz / nxy_len
    matrix[2, 1] = -nxy_len

    matrix[0, 2] = nx
    matrix[1, 2] = ny
    matrix[2, 2] = nz

    # Add the translation part of the matrix (detected center position of the catheter)
    matrix[0, 3] = pos[0]
    matrix[1, 3] = pos[1]
    matrix[2, 3] = pos[2]

    return matrix


# =================================================================================
# This function analyses a tracking projection, determines peak poisitions,
# SNR etc. If one tracking shot is complete (projection data from each coil and
# each direction are available), the coil positions in XYZ space are determined,
# and a transformation matrix is sent to Slicer
# =================================================================================
def tracking_data_to_slicer(message, images, scan_names, shotList):
    global ord_number
    global last_dyn
    global last_slice_number

    if message.pixel_data_type == mrtc_pb2.PixelDataType.PIXEL_DATA_TYPE_MAGNITUDE:
        num_rows = message.rows
        num_cols = message.columns

        array_size = num_rows * num_cols
        projection = np.frombuffer(message.pixel_data, dtype=np.uint16, count=array_size)
        projection = projection.reshape((num_rows, num_cols))

        # Record the order in which these data have arrived, this matches the channel number.
        # In future releases of MRTC, the channel number will be encoded in the MRTC message.
        if (message.dynamic_scan_number == last_dyn) and (message.slice_number == last_slice_number):
            ord_number = ord_number + 1
        else:
            ord_number = 0

        last_dyn = message.dynamic_scan_number
        last_slice_number = message.slice_number

        ori = oriMap[message.slice_number]
        cath = chMap[ord_number][0]
        coil = chMap[ord_number][1]
        readDirection = np.array((message.slice_orientation.row_direction_cosines.x, message.slice_orientation.row_direction_cosines.y, message.slice_orientation.row_direction_cosines.z))
        centerPointUpperLeftVoxel_mm = np.array((message.center_point_of_upper_left_voxel_mm.x, message.center_point_of_upper_left_voxel_mm.y, message.center_point_of_upper_left_voxel_mm.z))

        # ========================================================================================================================
        # Annoyingly, the readout orientation vector for one stack orientation (coronal) appears to be wrong (vector is swopped)
        # We correct this manually here until the SW bug in MRTC has been fixed.
        # ========================================================================================================================
        if ori == "cor":
            tmp = readDirection[2]
            readDirection[2] = readDirection[0]
            readDirection[0] = tmp
            tmp = centerPointUpperLeftVoxel_mm[2]
            centerPointUpperLeftVoxel_mm[2] = centerPointUpperLeftVoxel_mm[0]
            centerPointUpperLeftVoxel_mm[0] = tmp

        # ============================================================================================================================
        # While we're looking at each projection, we detect the peak position (along the readout direction), measure the SNR,
        # and log if this is a single- or dual peak (that is only needed for debugging, to try different algorithms)
        # ============================================================================================================================
        array_size = num_rows * num_cols
        projection = np.frombuffer(image_m.pixel_data, dtype=np.uint16, count=array_size)
        projection = projection.reshape((num_rows, num_cols))

        # ===========================================================================================================================================
        # Check the SNR. We look at a few samples at both extreme ends of the readout. We may still have some signal there, e.g. stemming
        # from a transformer, or if the catheter is at the edge of the FOV. However, these cannot be at both ends of the FOV at the same time.
        # Hence, we take the smaller one of the two numbers, that is our background noise.
        # ===========================================================================================================================================
        noise_l = np.mean(projection[0:50])
        noise_r = np.mean(projection[-50:-1])
        noise = min(noise_l, noise_r)

        # ==============================================
        # Find peak position along readout direction
        # ==============================================
        [peakPos_mm, peakVal, peakIdx, dualPeak] = findDualOrSinglePeak(projection, message.slice_dimensions_mm.x / num_rows)

        # ==================================================================================================================================
        # Calculate coil position ordinate. This is pure vector algebra (position in space = start position + length x direction vector)
        # ==================================================================================================================================
        coilPos_mm = -(centerPointUpperLeftVoxel_mm + peakPos_mm * readDirection)

        # =========================================================================================================================
        # Finally, fill the dictionary with all the information we have gathered while browsing through the current message
        # =========================================================================================================================
        curMutableDict[cath][coil][ori] = {
            "SNR": peakVal / noise,
            "coilPos_mm": coilPos_mm,
            "dualPeak": dualPeak,
            "peakIdx": peakIdx,
            "projection": projection,
        }

        # ==========================================================================================================================
        # Check if all orientations are present in all catheter and coil entries (=shot is complete)
        # If so, we can
        # (1) calculate the position in XYZ-space of each coil on each catheter
        # (2) do some sanity checks: is the SNR sufficient for all orthogonal readouts? Does the measured distance between the proximal and distal coil make sense?
        # (3) detect the catheter tip center point (in between proximal and distal coil), and the tip direction
        # (4) derive the transformation matrix for Slicer
        # (5) Complete our "tracking shot" dictionary, and append it to a list ("shotList")
        # =========================================================================================================================

        # OPTIMIZATION & BUG FIX:
        # 1. Hoisted 'set(oriMap)' outside the loop to avoid recreating the set N times.
        # 2. Used '.issubset()' directly on the dict to bypass '.keys()' and avoid implicit set conversions.
        # 3. Replaced '[cath, coil]' with tuple unpacking 'cath, coil' to reduce unpacking overhead.
        # 4. Added 'chMap and' to guard against the "all() trap" (= "vacuous truth" if chMap is empty).

        # IMPORTANT NOTE: # This temporary variable "ori_set" forgets indexing (while oriMap stays a tuple)
        ori_set = set(oriMap)
        if chMap and all(ori_set.issubset(curMutableDict[cath][coil]) for cath, coil in chMap):
            # First thing: convert this into a standard dictionary, and clear the original (mutable) dictionary for new incoming messages
            global trackingShotDict
            trackingShotDict = defaultdict_to_dict(curMutableDict)
            curMutableDict.clear()

            # Determine all coil positions in xyz-Space
            for [cath, coil] in chMap:
                trackingShotDict[cath][coil]["coilPositionXYZ"] = (trackingShotDict[cath][coil]["tra"]["coilPos_mm"] + trackingShotDict[cath][coil]["sag"]["coilPos_mm"] + trackingShotDict[cath][coil]["cor"]["coilPos_mm"])
                
                # Set receive timestamp
                trackingShotDict[cath]["timestamp"] = datetime.now()

            for cath in trackingShotDict.keys():
                # Center position between the coils
                trackingShotDict[cath]["centerPos"] = (trackingShotDict[cath]["dist"]["coilPositionXYZ"] + trackingShotDict[cath]["prox"]["coilPositionXYZ"]) / 2

                # Catheter tip direction vector
                trackingShotDict[cath]["dirVector"] = (trackingShotDict[cath]["dist"]["coilPositionXYZ"] - trackingShotDict[cath]["prox"]["coilPositionXYZ"])
                # Distance between coil (=length of direction vector)
                trackingShotDict[cath]["coilDistance"] = np.linalg.norm(trackingShotDict[cath]["dirVector"])

                # Normalise the direction vector (length 1)
                if trackingShotDict[cath]["coilDistance"] != 0:
                    trackingShotDict[cath]["dirVector"] /= trackingShotDict[cath]["coilDistance"]

                # Sanity check #1 (SNR)
                snrOK = all(snr > minSNRtoTrustPosition for snr in nested_lookup("SNR", trackingShotDict[cath]))

                # Sanity check #2 (distance between coils on catheter tip)
                distOK = (np.abs(trackingShotDict[cath]["coilDistance"]) < distanceBetweenCoils + margin)

                # Add validity flag to tracking shot dictionary
                trackingShotDict[cath]["valid"] = snrOK and distOK

                # Calc distance to last shot (note: not looking at validity)
                if len(shotList) > 0:
                    trackingShotDict[cath]["distanceToLastShot"] = np.linalg.norm(trackingShotDict[cath]["centerPos"] - shotList[-1][cath]["centerPos"])
                else:
                    trackingShotDict[cath]["distanceToLastShot"] = 0.0

                # ===================================================================================================================
                # From this point on, calculations are for 3D Slicer only. As agreed with clinical users, Slicer should receive
                # averaged (smoothed) catheter positions and tip orientations for improved maneuverablility.
                # As per discussion on Jun 1, 2026, we will send the tip position (i.s.o. center between coils) to Slicer
                # ===================================================================================================================

                if trackingShotDict[cath]["valid"]:                    
                    validPositionSeen[cath] = True
                    [avgCenterPos, avgDirVector] = smart_averaging_weighted_mean(cath, trackingShotDict, shotList, numAverages, maxAveragingDistance)
                    avgTipPos = avgCenterPos + (0.5 * distanceBetweenCoils + distanceBetweenTipAndNearestCoil) * avgDirVector                                    
                    lastGoodAvgTipPos[cath] = avgTipPos
                    lastGoodAvgDirVector[cath] = avgDirVector

                else:  
                    avgTipPos = lastGoodAvgTipPos[cath]
                    avgDirVector = lastGoodAvgDirVector[cath]
                    
                    
                # =======================================================
                # Send current transformation to Slicer and/or HoloLens
                # =======================================================
                if sendToSlicer and validPositionSeen[cath]:
                    transform_message = pyigtl.TransformMessage(pos_to_matrix(avgTipPos, avgDirVector), device_name=cath + "_TF", timestamp=time())
                    
                    transform_message.header_version = 2
                    transform_message.metadata = {"valid": str(trackingShotDict[cath]["valid"]),"tipPos": str(avgTipPos.tolist())}
                    server.send_message(transform_message)

                if sendToHoloLens and validPositionSeen[cath]:
                    send_catheter(len(shotList), cathID[cath], trackingShotDict[cath]["prox"]["coilPositionXYZ"], trackingShotDict[cath]["dist"]["coilPositionXYZ"], sock, udp_ip, cathPort[cath])

            # Add our current shot to the shot list. Be very careful with identation!
            shotList.append(copy.deepcopy(trackingShotDict))  # A deep copy ensures that nested dictionaries or mutable objects within the dictionary are also copied, not just referenced.


# ==============================================
# Establish connection to slicer, if requested
# ==============================================

if sendToSlicer:
    # "Slicer must be enabled as client, default port for OpenIGTLink is 18944"   
    server = pyigtl.OpenIGTLinkServer(port=18944, local_server=True)
    server.start()

# ==============================================
# Establish connection to HoloLens, if requested
# ==============================================

if sendToHoloLens:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# ==================================================
# Keep a flag wether we have seen a valid position,
# and the last known "good" position
# ==================================================

validPositionSeen = {}
lastGoodAvgTipPos = {}
lastGoodAvgDirVector = {}

for cath in caths:
    validPositionSeen[cath] = False
    lastGoodAvgTipPos[cath] = np.array([0.0, 0.0, 0.0])
    lastGoodAvgDirVector[cath] = np.array([1.0, 0.0, 0.0])


if replayPickleFile:
    # ==============================================
    # Load MR-data from a pre-recorded pickle file
    # ==============================================
    f = open(replayPickleFile, "rb")
    shotList = pickle.load(f)

    # In a fist round, we have to add the "distance to last shot" entry, because it might not yet be present in the recorded data
    for cath in shotList[0].keys():
        shotList[0][cath]["distanceToLastShot"] = 0

        for idx in range(1, len(shotList)):
            shotList[idx][cath]["distanceToLastShot"] = np.linalg.norm(shotList[idx][cath]["centerPos"] - shotList[idx - 1][cath]["centerPos"])
         
        # Count the number of invalid positions (blanks) for a coarse analysis of the tracking signal quality            
        c = Counter()
        for shot in shotList:
            c[shot[cath]["valid"]] += 1
        print(f"Catheter {cath} has {c[False]} invalid versus {c[True]} valid positions ({round(c[False]/len(shotList) * 100)}% invalid)")
            
    for shotIdx in range(1, len(shotList)):
        # Use simulated shotList (up to the current position) from the full shotList.
        # and simulated current shot (last element in array)
        for cath in caths:
            curShot = shotList[shotIdx]
            # Christian Test
            curShot[cath]["timestamp"] = datetime.now()
            if curShot[cath]["valid"]:
                validPositionSeen[cath] = True
                
                [avgCenterPos, avgDirVector] = smart_averaging_weighted_mean(cath, curShot, shotList[0:shotIdx], numAverages, maxAveragingDistance)
                avgTipPos = avgCenterPos + (0.5 * distanceBetweenCoils + distanceBetweenTipAndNearestCoil) * avgDirVector
                
                lastGoodAvgTipPos[cath] = avgTipPos
                lastGoodAvgDirVector[cath] = avgDirVector
                
                
            else:
                avgTipPos = lastGoodAvgTipPos[cath]
                avgDirVector = lastGoodAvgDirVector[cath]

            # =======================================================
            # Send current transformation to Slicer and/or HoloLens
            # =======================================================
            if sendToSlicer and validPositionSeen[cath]:
                transform_message = pyigtl.TransformMessage(pos_to_matrix(avgTipPos, avgDirVector), device_name=cath + "_TF", timestamp=time())
                
                transform_message.header_version = 2
                transform_message.metadata = {"valid": str(curShot[cath]["valid"]), "tipPos": str(avgTipPos.tolist())}                
                #print(transform_message)
                server.send_message(transform_message)
                sleep(0.05)



    # THIS PREVENTS THE 10054 ERROR UPON RE-RUN
    # ConnectionResetError [WinError 10054] occurs because the old network
    # socket from the previous script execution is still hanging open in the
    # operating system
    
    if "server" in locals() and server is not None:
        import threading
        print("Safely shutting down pyigtl connections...")
        # WinError 10038: Occurs because the main thread destroys the network
        # socket while a background thread is still actively polling it for data.
        # Override the thread exception handler to ignore the WinError 10038 noise
        def silent_thread_excepthook(args):
            if "10038" in str(args.exc_value) or "socket" in str(args.exc_type):
                return  # Ignore this specific shutdown race condition quietly
            threading.__excepthook__(args)  # Pass any real errors through

        threading.excepthook = silent_thread_excepthook

        # Stop the server safely
        server.stop()

        # Give Windows a moment to release port 18944
        sleep(0.1)


else:
    # ===================================================
    # The actual MRTC communication starts here
    # ===================================================

    # PingServiceProvider to Init Service
    print("\n--- PingServiceProvider to Init Service ---\n")
    m1 = mrtc_pb2.PingServiceProviderRequestMessage()
    m2 = mrtc_pb2.PingServiceProviderResponseMessage()
    message_type = struct.pack("<I", mrtc_pb2.MessageType.MESSAGE_TYPE_PING_SERVICE_PROVIDER_REQUEST)
    mrtc_func.send_and_receive_message(message_type, m1, m2, mrtc_func.init_serv_address, mrtc_func.init_serv_port)
    incarnation_token = m2.incarnation_token
    print("Incarnation token: "+ hex(int.from_bytes(incarnation_token, byteorder="big")).upper())

    # SyncSystemConfigRequest to the Init Service
    print("\n--- SyncSystemConfigRequest to the Init Service ---\n")
    m1 = mrtc_pb2.SyncSystemConfigRequestMessage()
    m2 = mrtc_pb2.SyncSystemConfigResponseMessage()
    message_type = struct.pack("<I", mrtc_pb2.MessageType.MESSAGE_TYPE_SYNC_SYSTEM_CONFIG_REQUEST)
    m1.allow_mr_research_features = True
    m1.ntp_service_address.host_name_or_ip_address = mrtc_func.ntp_server_address
    m1.ntp_service_address.port_number = mrtc_func.ntp_server_port
    dicom_storage_scp_addresses = m1.dicom_storage_scp_addresses.add()
    dicom_storage_scp_addresses.tcp_address.host_name_or_ip_address = mrtc_func.dicom_server_address
    dicom_storage_scp_addresses.tcp_address.port_number = mrtc_func.dicom_server_port
    dicom_storage_scp_addresses.ae_title = mrtc_func.dicom_aetitle
    m1.scan_control_service_address.host_name_or_ip_address = mrtc_func.tc_scan_serv_address
    m1.scan_control_service_address.port_number = mrtc_func.tc_scan_serv_port
    m1.local_windows_time_zone_id = mrtc_func.get_local_time_zone_id()
    m1.philips_exam_card_database_version = database_version
    mrtc_func.send_and_receive_message(message_type, m1, m2, mrtc_func.init_serv_address, mrtc_func.init_serv_port)
    config_token = m2.config_token
    exam_serv_address = m2.exam_service_address.host_name_or_ip_address
    exam_serv_port = m2.exam_service_address.port_number
    user_serv_address = m2.user_service_address.host_name_or_ip_address
    user_serv_port = m2.user_service_address.port_number
    scan_serv_address = m2.scan_service_address.host_name_or_ip_address
    scan_serv_port = m2.scan_service_address.port_number
    print("Config token: " + hex(int.from_bytes(config_token, byteorder="big")).upper())
    print(f"Exam Service: {exam_serv_address}:{exam_serv_port}")
    print(f"User Service: {user_serv_address}:{user_serv_port}")
    print(f"Scan Service: {scan_serv_address}:{scan_serv_port}")

    # SyncActiveUserRequest to the User Service
    print("\n--- SyncActiveUserRequest to the User Service ---\n")
    m1 = mrtc_pb2.SyncActiveUserRequestMessage()
    m2 = mrtc_pb2.SyncActiveUserResponseMessage()
    message_type = struct.pack("<I", mrtc_pb2.MessageType.MESSAGE_TYPE_SYNC_ACTIVE_USER_REQUEST)
    m1.user_id = mrtc_func.active_user
    mrtc_func.send_and_receive_message(message_type, m1, m2, user_serv_address, user_serv_port)
    user_token = m2.user_token
    print("User token: " + hex(int.from_bytes(user_token, byteorder="big")).upper())

    # GetHospitalExamCardsInfoRequest to the Init Service
    print("\n--- GetHospitalExamCardsInfoRequest to the Init Service ---\n")
    m1 = mrtc_pb2.GetHospitalExamCardsInfoRequestMessage()
    m2 = mrtc_pb2.GetHospitalExamCardsInfoResponseMessage()
    message_type = struct.pack("<I", mrtc_pb2.MessageType.MESSAGE_TYPE_GET_HOSPITAL_EXAM_CARDS_INFO_REQUEST)
    mrtc_func.send_and_receive_message(message_type, m1, m2, mrtc_func.init_serv_address, mrtc_func.init_serv_port)
    
    for exam_card in m2.exam_cards:
        # Access each item in the repeated field
        print(f"ExamCard: {exam_card.id.path} (signature: {exam_card.id.signature})")
        for scan_protocol in exam_card.scan_protocols:
            if scan_protocol.remote_controllable:
                print(f"    - {scan_protocol.name:40} (MRTC enabled)")
            else:
                print(f"    - {scan_protocol.name:40}")

    # StartExamRequest to the Exam Service
    print("\n--- StartExamRequest to the Exam Service ---\n")
    m1 = mrtc_pb2.StartExamRequestMessage()
    m2 = mrtc_pb2.StartExamResponseMessage()
    message_type = struct.pack("<I", mrtc_pb2.MessageType.MESSAGE_TYPE_START_EXAM_REQUEST)
    m1.config_token = config_token
    m1.user_token = user_token
    m1.patient_data.patient_name = "MRTC test Leipzig"
    m1.patient_data.registration_id = "007"
    m1.patient_data.date_of_birth.year = 1975
    m1.patient_data.date_of_birth.month = 12
    m1.patient_data.date_of_birth.day = 23
    m1.patient_data.gender = mrtc_pb2.Gender.GENDER_MALE
    m1.patient_data.patient_weight_kg = 90
    m1.therapy_mode = mrtc_pb2.TherapyMode.THERAPY_MODE_RESEARCH
    m1.patient_position = mrtc_pb2.PatientPosition.PATIENT_POSITION_HEAD_FIRST_SUPINE
    m1.exam_card_id.repository = (mrtc_pb2.ExamCardRepository.EXAM_CARD_REPOSITORY_HOSPITAL_MRTC_FOLDER)
    m1.exam_card_id.path = mrtc_func.exam_card_id_path
    m1.exam_card_id.signature = b""
    mrtc_func.send_and_receive_message(message_type, m1, m2, exam_serv_address, exam_serv_port)
    exam_token = m2.exam_token
    print("Exam token: " + hex(int.from_bytes(exam_token, byteorder="big")).upper())

    # ==========================================================================================================
    # StartScanRequest to the Scan Service, with a tracking scan with real-time image sending
    # Put this into a loop. We may have to start silent tracking over and over (without closing the ExamCard)
    #===========================================================================================================

    keyIn = ""

    while keyIn != "s" and keyIn != "S":
        protocol_name = "SilentTracking"
        m1 = mrtc_pb2.StartScanRequestMessage()
        m2 = mrtc_pb2.TakeScanControlRequestMessage()
        m3 = mrtc_pb2.TakeScanControlResponseMessage()
        m4 = mrtc_pb2.StartScanResponseMessage()
        message_type1 = struct.pack("<I", mrtc_pb2.MessageType.MESSAGE_TYPE_START_SCAN_REQUEST)
        message_type3 = struct.pack("<I", mrtc_pb2.MessageType.MESSAGE_TYPE_TAKE_SCAN_CONTROL_RESPONSE)
        m1.exam_token = exam_token
        m1.scan_protocol_name = protocol_name

        sc_socket = mrtc_func.start_scan_and_take_scan_control_messages(message_type1, message_type3, m1, m2, m3, m4, scan_serv_address, scan_serv_port)
        scan_token = m4.scan_token
        remaining_scan_time = m4.approximate_remaining_scan_time_in_seconds
        print("Scan token: " + hex(int.from_bytes(scan_token, byteorder="big")).upper())
        print(f"Remaining scan time is {round(remaining_scan_time)} seconds")

        # Receive ImageData from the Scan service
        print("\n--- Receive ImageData from the Scan service ---")
        image_m = mrtc_pb2.ImageDataMessage()
        m1 = mrtc_pb2.ReleaseScanControlRequestMessage()
        m2 = mrtc_pb2.ReleaseScanControlResponseMessage()
        m3 = mrtc_pb2.SetStackPositionsRequestMessage()
        m4 = mrtc_pb2.SetStackPositionsResponseMessage()
        receive_message(sc_socket, image_m, m1, m2, m3, m4, 2)
        keyIn = input("Press Enter to restart tracking scan. Press ""s"" key + Enter to stop the loop: ")

    # ==================================
    # Save data into a pickle file
    # ==================================
    now = datetime.now()
    file = f"$home/tracking_data_{now.strftime('%Y-%m-%d_%H-%M-%S')}.pkl"
    f = open(file, "wb")
    pickle.dump(shotList, f)
    f.close()
    print(f"Tracking data were saved to {file}")
