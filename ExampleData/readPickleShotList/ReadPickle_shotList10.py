from time import time, sleep
import numpy as np
import pickle
from collections import deque


# ===============================================================================
# This is for creating, converting  and searching mutable, nested dictionaries
# ===============================================================================
from collections import defaultdict
from typing import Any

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
# Anschluss der Katheterleitungen an die Interface-Box: die Eingaenge sind von links nach rechts nummeriert. Dabei kommt an
#
# 1 - Abl distal
# 2 - Abl proximal
# 3 - Diag distal
# 4 - Diag proximal
# ========================================================================================================================================
chMap = (
    ("abl", "dist"),  # ch0
    ("abl", "prox"),  # ch1
    ("ref", "dist"),  # ch2
    ("ref", "prox"),
)  # ch3


# ==================================================================
# This maps the catheter names onte Sandra Haltmeier's catheter IDs
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
    "cor",
)  # slice 2

# ==================================================================
# This maps the catheter names onte Sandra Haltmeier's catheter IDs
# ==================================================================
# cathID = {caths[1]: 0, caths[0]: 1}

# ==================================================================
# This maps the catheter names onto the port numbers for the
# communication with HoloLens
# ==================================================================
# cathPort = {caths[0]: 5006, caths[1]: 5005}


# =================================================================================================================================================================================
# Entries from xml-configuation for Vision MR catheters (Perhaps we should just parse the xml-File)
# <DualCoilCatheter MaxExtent="5.0" Name="Vision MR" DeviceHardware="DualCoilCatheter" Diameter="3" DistanceBetweenCoils="5.8" DistanceBetweenTipAndNearestCoil="8.1"/>
# =================================================================================================================================================================================
noiseThreshold = 200  # Threshold above which we assume that this is a real signal
minSNRtoTrustPosition = 10  # Minimum required SNR to trust a peak position (who knew?)
distanceBetweenCoils = 9  # distance between coil elements in mm (was 7.77)
distanceBetweenTipAndNearestCoil = 8.1  # distance between the distal coil and the catheter tip
margin = 4.0  # allowed tolerance of distance between coils (was 2.0)
maxExtent = 2.0  # Maximum width of a peak (sensitivity profile of microcoil)
maxRelPeakDiff = 20  # Maximum relative difference between peaks to accept them as a peak pair left and right of the actual coil
maxAveragingDistance = 30  # If the catheter is moved very quickly, averaging may result in the catheter apparantly taking an "illegal shortcut" (direct path between positions). Turn averaging off in those cases.


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
# ================================================
# Replay data from pickle file?
# ================================================
# replayPickleFile = ""
replayPickleFile = r"./tracking_data_2025-11-06_13-35-02_repaired_shortened_600_5000.pkl"


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
curMutableDict = makehash()  # A mutable, nested dictionary, will be filled on the fly as the data come in
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
    if (voxelSizeM * (peakIdxs[1] - peakIdxs[0]) < maxExtent) and (
        (rawData[peakIdxs[1]] - rawData[peakIdxs[0]]) / rawData[peakIdxs[1]] < (maxRelPeakDiff / 100)
    ):
        # If so, locate the catheter between the peaks using a center of mass approach
        peakPos = np.average(peakIdxs, 0, rawData[peakIdxs].flatten()) * voxelSizeM
        dualPeak = True

    peakVal = rawData[peakIdxs[1]]
    return (peakPos, peakVal, peakIdxs[1], dualPeak)


# ==================================================================================
# Sandra's function to send the catheter coil positions to the HoloLens via UDP
# ==================================================================================
def send_catheter(timestamp, catheter_id, proximal_xyz, distal_xyz, sock, udp_ip, udp_port):
    """
    two strings for the two catheters are sent to two different ports via UDP
    timestap: sequence number of package
    catheter_id: 0 (green) or 1 (red)
    proximal_xyz: [x, y, z] coordinates of proximal coil (coil at lower end of tip)
    distal_xyz: [x, y, z] coordinate of distal coil (coil at upper end of tip)
    """
    message = f"{int(timestamp)},{catheter_id},{proximal_xyz[0]},{proximal_xyz[1]},{proximal_xyz[2]},{distal_xyz[0]},{distal_xyz[1]},{distal_xyz[2]}".encode(
        "utf-8"
    )
    sock.sendto(message, (udp_ip, udp_port))


def smart_averaging(cath, shot, shotList, numAverages, maxAveragingDistance):
    # Initialize the stateful counter on the function object if it doesn't exist yet
    if not hasattr(smart_averaging, "consecutive_errors"):
        smart_averaging.consecutive_errors = 0

    # Max allowed sequential drops before we declare a total telemetry blackout
    MAX_ALLOWED_DROPS = 2

    # Initialize return variables to prevent UnboundLocalError/KeyError if frame is missing or invalid
    avgCenterPos = {}
    avgDirVector = {}

    # 1. Handle Invalid Live Frames immediately to increment our error counter
    if not shot.get(cath, {}).get("valid", False):
        smart_averaging.consecutive_errors += 1
        return (avgCenterPos, avgDirVector)

    # 2. Check if we are recovering from a major telemetry blackout
    if smart_averaging.consecutive_errors > MAX_ALLOWED_DROPS:
        # Reset the counter and calculate a crisp, raw real-time position bypass
        # to prevent pulling in stale historical data vectors across the blackout gap
        smart_averaging.consecutive_errors = 0

        avgCenterPos = (np.array(shot[cath]["dist"]["coilPositionXYZ"]) + np.array(shot[cath]["prox"]["coilPositionXYZ"])) / 2.0
        avgDirVector = np.array(shot[cath]["dist"]["coilPositionXYZ"]) - np.array(shot[cath]["prox"]["coilPositionXYZ"])

        coilDistance = np.linalg.norm(avgDirVector)
        if coilDistance > 0:
            avgDirVector /= coilDistance
        else:
            avgDirVector = np.array([0.0, 0.0, 0.0])

        return (avgCenterPos, avgDirVector)

    # If the frame is valid and we aren't recovering from a blackout, clear error streak
    smart_averaging.consecutive_errors = 0

    if shot[cath]["valid"]:
        # =========================================================================
        # Smart averaging: average the last N shots, but only valid shots.
        # Also, stop at a position that is very far away from the previous
        # position (catheter had been moved very quickly). Otherwise, the catheter
        # is apparently taking an "illegal" trajectory between measured positions
        # =========================================================================
        coils = ["dist", "prox"]

        # Wrapped inside np.array immediately to guarantee correct matrix mathematics inside np.sum
        coilPositionsXYZ = {coil: [np.array(shot[cath][coil]["coilPositionXYZ"])] for coil in coils}

        # Start loop at 2 to skip current shot at shotList[-1]
        for idx in range(2, min(numAverages + 1, len(shotList) + 1)):
            hist_shot = shotList[-idx]

            # Break logic: Stop evaluating history completely if a gap is too wide
            if hist_shot.get(cath, {}).get("distanceToLastShot", 0.0) > maxAveragingDistance:
                break  # stop here

            # Continue logic: Skip invalid historical frames but continue checking older ones
            if not hist_shot.get(cath, {}).get("valid", False):
                continue  # skip this invalid shot

            # Process valid historical data
            for coil in coils:
                coilPositionsXYZ[coil].append(np.array(hist_shot[cath][coil]["coilPositionXYZ"]))

        avgCoilPositionXYZ = {}
        for coil in coils:
            avgCoilPositionXYZ[coil] = np.sum(coilPositionsXYZ[coil], axis=0) / len(coilPositionsXYZ[coil])

        # ==========================================================================================================
        # Re-calculate center (reference) positions and direction vectors for slicer matrix based on smart average
        # ==========================================================================================================
        avgCenterPos = (avgCoilPositionXYZ["dist"] + avgCoilPositionXYZ["prox"]) / 2.0
        avgDirVector = avgCoilPositionXYZ["dist"] - avgCoilPositionXYZ["prox"]
        coilDistance = np.linalg.norm(avgDirVector)
        if coilDistance != 0:
            avgDirVector /= coilDistance

    return (avgCenterPos, avgDirVector)

    # ==========================================================================================
    # FUNCTION DOCUMENTATION: Velocity-Adaptive Exponential Moving Average (EMA) Filters
    # ==========================================================================================
    # This documentation covers two architectural variations that implement a industry-standard
    # 'Velocity-Adaptive Exponential Moving Average (EMA)' framework to smooth 3D sensor
    # coordinates by dynamically scaling filter coefficients based on real-time catheter velocity.
    #
    # THE DESIGN PROBLEM:
    # In 10 Hz sensor systems (100 ms per frame), the human eye easily catches the violent visual
    # jumping or "twitching" caused by rigid binary state-switching (like hard break loops).
    # Traditional historical averaging creates severe "rubber-band" lag when the device moves,
    # while dropping history instantly creates a jarring, noisy snap onto raw coordinates.
    #
    # THE APPLIED SOLUTION:
    # To make the catheter move with organic, fluid grace without reintroducing heavy lag,
    # both functions utilize a velocity-adaptive EMA model to replace the rigid break switch
    # with a continuously blending transition scale. Instead of cutting history off like a
    # cliff, they evaluate 'distanceToLastShot' relative to 'maxAveragingDistance' to compute a
    # dynamic smoothing factor ('alpha') that cross-fades between the 'historicalAverage' of
    # past valid shots and the real-time frame data.
    #
    # ==========================================================================================
    # VARIATION 1: smart_averaging_fluid (Linear Adaptive EMA)
    # ==========================================================================================
    #  - Mathematical Model: Uses a direct, linear 'speed_ratio' calculation to step the EMA weight.
    #  - Kinematic Behavior: The Adaptive EMA 'alpha' scaling parameter progresses in a uniform,
    #    straight line from 'base_alpha' (0.2) up to 1.0 as velocity increases.
    #  - Visual Feel: Highly predictable and steady response. Best suited for environments
    #    where hand acceleration is mechanical, uniform, or highly controlled.
    #  - Static State: When still, alpha stays locked at 0.2, heavily relying on history to
    #    dampen static electronic jitter via strict history-weighted smoothing.
    #  - Dynamic State: As 'distanceToLastShot' matches or exceeds 'maxAveragingDistance',
    #    the Adaptive EMA completely shifts tracking focus onto raw, lag-free real-time data.
    #
    # ==========================================================================================
    # VARIATION 2: smart_averaging_fluid_exponential (Nonlinear Exponential Adaptive EMA)
    # ==========================================================================================
    #  - Mathematical Model: Governs the Adaptive EMA via an exponential decay curve (np.exp)
    #    scaled by a tuned 'sensitivity' multiplier.
    #  - Kinematic Behavior: The Adaptive EMA 'alpha' scales nonlinearly. At the slightest hint of
    #    intentional movement, the algorithm sheds historical data at a rapid, exponential rate,
    #    rounding out smoothly along a natural logarithmic-style path as it reaches top speed.
    #  - Visual Feel: Matches natural human hand kinematics beautifully. The transition boundary
    #    between resting stabilization and high-speed raw tracking becomes completely invisible,
    #    eliminating all mechanical steps or "perceptible shifts" in filtering power.
    #  - Static State: When still, the Adaptive EMA drops alpha down to 0.2 via exponential relaxation.
    #  - Dynamic State: Rapidly approaches 1.0 under quick acceleration, decoupling history
    #    instantly to provide an ultra-responsive visual track during fast repositioning.
    #
    # ==========================================================================================
    # RETURNS (Both Functions):
    #   Tuple[dict, dict]: (avgCenterPos, avgDirVector)
    #   Returns empty dictionaries ({}, {}) safely if the current shot is marked invalid.
    # ==========================================================================================


def smart_averaging_weighted_mean(cath, shot, shotList, numAverages, maxAveragingDistance):
    # Initialize the stateful counter on the function object if it doesn't exist yet
    if not hasattr(smart_averaging_weighted_mean, "consecutive_errors"):
        smart_averaging_weighted_mean.consecutive_errors = 0

    # Max allowed sequential drops before we declare a total telemetry blackout
    MAX_ALLOWED_DROPS = 2

    # Initialize return variables to prevent UnboundLocalError if shot[cath]["valid"] is False
    avgCenterPos = {}
    avgDirVector = {}

    # 1. Handle Invalid Live Frames immediately to increment our error counter
    if not shot.get(cath, {}).get("valid", False):
        smart_averaging_weighted_mean.consecutive_errors += 1
        return (avgCenterPos, avgDirVector)

    # 2. Check if we are recovering from a major telemetry blackout
    if smart_averaging_weighted_mean.consecutive_errors > MAX_ALLOWED_DROPS:
        # Reset the counter and calculate a crisp, raw real-time position bypass
        # to prevent pulling in stale historical data vectors across the blackout gap
        smart_averaging_weighted_mean.consecutive_errors = 0

        avgCenterPos = (np.array(shot[cath]["dist"]["coilPositionXYZ"]) + np.array(shot[cath]["prox"]["coilPositionXYZ"])) / 2.0
        avgDirVector = np.array(shot[cath]["dist"]["coilPositionXYZ"]) - np.array(shot[cath]["prox"]["coilPositionXYZ"])

        coilDistance = np.linalg.norm(avgDirVector)
        if coilDistance > 0:
            avgDirVector /= coilDistance
        else:
            avgDirVector = np.array([0.0, 0.0, 0.0])

        return (avgCenterPos, avgDirVector)

    # If the frame is valid and we aren't recovering from a blackout, clear error streak
    smart_averaging_weighted_mean.consecutive_errors = 0

    if shot[cath]["valid"]:
        # =========================================================================
        # Smart averaging: average the last N shots, but only valid shots.
        # Also, stop at a position that is very far away from the previous
        # position (catheter had been moved very quickly). Otherwise, the catheter
        # is apparently taking an "illegal" trajectory between measured positions
        # =========================================================================
        coils = ["dist", "prox"]
        avgCoilPositionXYZ = {}

        for coil in coils:
            # Init (list) using explicit arrays to prevent typing conflicts downstream
            posVector = [np.array(shot[cath][coil]["coilPositionXYZ"])]
            # Initialized as a 3D zero vector array to satisfy the Ruff linter rules
            distVector = [np.array([0.0, 0.0, 0.0])]

            # Start loop at 2 to skip current shot at shotList[-1]
            for idx in range(2, min(numAverages + 1, len(shotList) + 1)):
                hist_shot = shotList[-idx]

                # Break logic: Stop evaluating history completely if a gap is too wide
                if hist_shot.get(cath, {}).get("distanceToLastShot", 0.0) > maxAveragingDistance:
                    break  # stop here

                # Continue logic: Skip invalid historical frames but continue checking older ones
                if not hist_shot.get(cath, {}).get("valid", False):
                    continue  # skip this invalid shot

                # Process valid historical data
                posVector.append(np.array(hist_shot[cath][coil]["coilPositionXYZ"]))
                # Safe array subtraction prevents TypeErrors with native Python lists
                distVector.append(posVector[-1] - posVector[0])

            # Offset to avoid zeros in weight denominator
            offset = 5
            weightsVector = [1 / (np.linalg.norm(distVector[i]) + offset) for i in range(len(distVector))]

            avgCoilPositionXYZ[coil] = np.average(posVector, weights=weightsVector, axis=0)

        avgCenterPos = (avgCoilPositionXYZ["dist"] + avgCoilPositionXYZ["prox"]) / 2.0
        avgDirVector = avgCoilPositionXYZ["dist"] - avgCoilPositionXYZ["prox"]
        coilDistance = np.linalg.norm(avgDirVector)
        if coilDistance != 0:
            avgDirVector /= coilDistance

    return (avgCenterPos, avgDirVector)


def smart_averaging_IDW(cath, shot, shotList, numAverages, maxAveragingDistance):
    """
    Applies Real-Time Velocity-Adaptive Inverse Distance Weighting (IDW) to smooth catheter telemetry tracking.

    Behavioral Architecture:
    1. Stateful Telemetry Guard:
       Tracks sequential dropped or invalid frames. If a telemetry blackout occurs (drops > 2),
       it clears historical buffers to instantly bypass lag and output raw real-time coordinates.

    2. Dynamic Memory Architecture:
       Maintains internal persistent deques attached directly to the function namespace. It automatically
       re-allocates and resizes historical horizons at runtime if the 'numAverages' threshold changes.

    3. Spatial IDW Math Core:
       Measures physical Euclidean distances between the incoming real-time position and cached history points.
       Applies standard spatial IDW weighting [ 1 / (distance + offset) ] to favor nearby localized frames
       and prevent trailing coordinate lag during navigation.

    4. Velocity-Capped Jump Filtering:
       Measures sudden coordinate jumps against 'maxAveragingDistance'. If the movement velocity spike
       exceeds this threshold, history processing is temporarily shut down to prevent the catheter from
       rendering an mathematically generated "illegal trajectory shortcut" during sudden real-world shifts.

    Returns:
        tuple: (avgCenterPos, avgDirVector)
               - avgCenterPos: Dict containing the computed 3D spatial midpoint array.
               - avgDirVector: Dict containing the normalized 3D directional unit vector array.
    """
    # Initialize the stateful counter on the function object if it doesn't exist yet
    if not hasattr(smart_averaging_IDW, "consecutive_errors"):
        smart_averaging_IDW.consecutive_errors = 0

    # Max allowed sequential drops before we declare a total telemetry blackout
    MAX_ALLOWED_DROPS = 2

    # Initialize return variables to prevent UnboundLocalError if shot[cath]["valid"] is False
    avgCenterPos = {}
    avgDirVector = {}
    coils = ["dist", "prox"]

    # 1. Handle Invalid Live Frames immediately to increment our error counter
    if not shot.get(cath, {}).get("valid", False):
        smart_averaging_IDW.consecutive_errors += 1
        return (avgCenterPos, avgDirVector)

    # 2. Check if we are recovering from a major telemetry blackout
    if smart_averaging_IDW.consecutive_errors > MAX_ALLOWED_DROPS:
        # Reset the counter and calculate a crisp, raw real-time position bypass
        # to prevent pulling in stale historical data vectors across the blackout gap
        smart_averaging_IDW.consecutive_errors = 0

        # Clear any stored ring buffer history on blackout recovery to ensure smooth re-locking
        if hasattr(smart_averaging_IDW, "_filter_instance"):
            delattr(smart_averaging_IDW, "_filter_instance")

        avgCenterPos = (np.array(shot[cath]["dist"]["coilPositionXYZ"]) + np.array(shot[cath]["prox"]["coilPositionXYZ"])) / 2.0
        avgDirVector = np.array(shot[cath]["dist"]["coilPositionXYZ"]) - np.array(shot[cath]["prox"]["coilPositionXYZ"])

        coilDistance = np.linalg.norm(avgDirVector)
        if coilDistance > 0:
            avgDirVector /= coilDistance
        else:
            avgDirVector = np.array([0.0, 0.0, 0.0])

        return (avgCenterPos, avgDirVector)

    # If the frame is valid and we aren't recovering from a blackout, clear error streak
    smart_averaging_IDW.consecutive_errors = 0

    # Initialize or dynamically update the persistent filter settings if values change mid-stream
    if not hasattr(smart_averaging_IDW, "_filter_instance"):
        smart_averaging_IDW._filter_instance = type("FilterState", (), {})()
        smart_averaging_IDW._filter_instance.buffers = {"dist": deque(maxlen=numAverages), "prox": deque(maxlen=numAverages)}

    # Enforce updated maxlen dynamically if numAverages changes during runtime
    for coil in coils:
        if smart_averaging_IDW._filter_instance.buffers[coil].maxlen != numAverages:
            # Recreate deque while preserving existing historical elements
            existing_elements = list(smart_averaging_IDW._filter_instance.buffers[coil])
            smart_averaging_IDW._filter_instance.buffers[coil] = deque(existing_elements, maxlen=numAverages)

    # Target the persistent buffer reference
    history_buffers = smart_averaging_IDW._filter_instance.buffers
    avgCoilPositionXYZ = {}
    offset = 5.0  # Safe default smoothing parameter matching your class definition

    # =========================================================================
    # True Spatial Inverse Distance Weighting (IDW) Core Math Block
    # =========================================================================
    for coil in coils:
        current_pos = np.array(shot[cath][coil]["coilPositionXYZ"])

        # Init spatial vectors with just the current frame data
        posVector = [current_pos]
        distVector = [np.array([0.0, 0.0, 0.0])]

        # --- Velocity Guard: Check for large jumps to turn off averaging ---
        jump_distance = 0.0
        if len(history_buffers[coil]) > 0:
            previous_valid_pos = history_buffers[coil][0]  # Leftmost element is the latest frame
            jump_distance = np.linalg.norm(current_pos - previous_valid_pos)

        # Only pull in localized historical coordinates if movement stays under the threshold
        if jump_distance <= maxAveragingDistance:
            for hist_pos in history_buffers[coil]:
                posVector.append(hist_pos)
                distVector.append(hist_pos - current_pos)

        # --- Distance Weighting Inversion Calculation ---
        weightsVector = [1.0 / (np.linalg.norm(distVector[i]) + offset) for i in range(len(distVector))]

        # Blend spatial coordinates using true normalized IDW weights
        avgCoilPositionXYZ[coil] = np.average(posVector, weights=weightsVector, axis=0)

        # Internal State Update: Cache this valid frame into the ring-buffer for the next cycle
        history_buffers[coil].appendleft(current_pos)

    # --- Geometric Slicer Transformation Vector Feature Extraction ---
    avgCenterPos = (avgCoilPositionXYZ["dist"] + avgCoilPositionXYZ["prox"]) / 2.0
    avgDirVector = avgCoilPositionXYZ["dist"] - avgCoilPositionXYZ["prox"]
    coilDistance = np.linalg.norm(avgDirVector)

    if coilDistance != 0:
        avgDirVector /= coilDistance

    return (avgCenterPos, avgDirVector)


# ==========================================================================================
# FUNCTION DOCUMENTATION: Velocity-Adaptive Exponential Moving Average (EMA) Filters
# ==========================================================================================
# This documentation covers two architectural variations that implement an industry-standard
# 'Velocity-Adaptive Exponential Moving Average (EMA)' framework to smooth 3D sensor
# coordinates by dynamically scaling filter coefficients based on real-time catheter velocity.
#
# THE DESIGN PROBLEM:
# In 10 Hz sensor systems (100 ms per frame), the human eye easily catches the violent visual
# jumping or "twitching" caused by rigid binary state-switching (like hard break loops).
# Traditional historical averaging creates severe "rubber-band" lag when the device moves,
# while dropping history instantly creates a jarring, noisy snap onto raw coordinates.
#
# THE APPLIED SOLUTION:
# To make the catheter move with organic, fluid grace without reintroducing heavy lag,
# both functions utilize a velocity-adaptive EMA model to replace the rigid break switch
# with a continuously blending transition scale. Instead of cutting history off like a
# cliff, they evaluate 'distanceToLastShot' relative to 'maxAveragingDistance' to compute a
# dynamic smoothing factor ('alpha') that cross-fades between the 'historicalAverage' of
# past valid shots and the real-time frame data.
#
# ==========================================================================================
# VARIATION 1: smart_averaging_fluid (Linear Adaptive EMA)
# ==========================================================================================
#  - Mathematical Model: Uses a direct, linear 'speed_ratio' calculation to step the EMA weight.
#  - Kinematic Behavior: The Adaptive EMA 'alpha' scaling parameter progresses in a uniform,
#    straight line from 'base_alpha' (0.2) up to 1.0 as velocity increases.
#  - Visual Feel: Highly predictable and steady response. Best suited for environments
#    where hand acceleration is mechanical, uniform, or highly controlled.
#  - Static State: When still, alpha stays locked at 0.2, heavily relying on history to
#    dampen static electronic jitter via strict history-weighted smoothing.
#  - Dynamic State: As 'distanceToLastShot' matches or exceeds 'maxAveragingDistance',
#    the Adaptive EMA completely shifts tracking focus onto raw, lag-free real-time data.
#
# ==========================================================================================
# VARIATION 2: smart_averaging_fluid_exponential (Nonlinear Exponential Adaptive EMA)
# ==========================================================================================
#  - Mathematical Model: Governs the Adaptive EMA via an exponential decay curve (np.exp)
#    scaled by a tuned 'sensitivity' multiplier.
#  - Kinematic Behavior: The Adaptive EMA 'alpha' scales nonlinearly. At the slightest hint of
#    intentional movement, the algorithm sheds historical data at a rapid, exponential rate,
#    rounding out smoothly along a natural logarithmic-style path as it reaches top speed.
#  - Visual Feel: Matches natural human hand kinematics beautifully. The transition boundary
#    between resting stabilization and high-speed raw tracking becomes completely invisible,
#    eliminating all mechanical steps or "perceptible shifts" in filtering power.
#  - Static State: When still, the Adaptive EMA drops alpha down to 0.2 via exponential relaxation.
#  - Dynamic State: Rapidly approaches 1.0 under quick acceleration, decoupling history
#    instantly to provide an ultra-responsive visual track during fast repositioning.
#
# ==========================================================================================
# RETURNS (Both Functions):
#   Tuple[np.ndarray, np.ndarray]: (avgCenterPos, avgDirVector)
#   Returns safe fallbacks (None, None) if the current shot is marked invalid.
# ==========================================================================================


def smart_averaging_fluid(cath, shot, shotList, numAverages, maxAveragingDistance):
    avgCenterPos = {}
    avgDirVector = {}

    if shot[cath]["valid"]:
        # =========================================================================
        # Smart averaging: average the last N shots, but only valid shots.
        # Instead of cutting history off like a cliff with a rigid break switch,
        # it smoothly cross-fades between the history and the real-time position
        # based on how fast you are moving to provide organic, fluid grace.
        # =========================================================================

        # Calculate dynamic smooth-blending weight (Adaptive EMA alpha parameter)
        distanceToLastShot = shot[cath].get("distanceToLastShot", 0.0)
        speed_ratio = min(max(distanceToLastShot / maxAveragingDistance, 0.0), 1.0)

        base_alpha = 0.2
        alpha = base_alpha + (1.0 - base_alpha) * speed_ratio

        # Gather history coordinates (no break statement, just filtering valid shots)
        coilPositionsXYZ = {coil: [] for coil in coils}

        for idx in range(1, min(numAverages, len(shotList))):
            if shotList[-idx][cath]["valid"]:
                for coil in coils:
                    coilPositionsXYZ[coil].append(shotList[-idx][cath][coil]["coilPositionXYZ"])

        avgCoilPositionXYZ = {}
        for coil in coils:
            currentPosition = np.array(shot[cath][coil]["coilPositionXYZ"])

            if coilPositionsXYZ[coil]:
                # Calculate simple arithmetic mean of the historical valid shots
                historicalAverage = sum(np.array(p) for p in coilPositionsXYZ[coil]) / len(coilPositionsXYZ[coil])
                # Smoothly blend present and past using the dynamic alpha scale
                avgCoilPositionXYZ[coil] = (alpha * currentPosition) + ((1.0 - alpha) * historicalAverage)
            else:
                avgCoilPositionXYZ[coil] = currentPosition

        # ==========================================================================================================
        # Re-calculate center (reference) positions and direction vectors for slicer matrix based on smart average
        # ==========================================================================================================
        avgCenterPos = (avgCoilPositionXYZ["dist"] + avgCoilPositionXYZ["prox"]) / 2
        avgDirVector = avgCoilPositionXYZ["dist"] - avgCoilPositionXYZ["prox"]
        coilDistance = np.linalg.norm(avgDirVector)
        if coilDistance != 0:
            avgDirVector /= coilDistance

    return (avgCenterPos, avgDirVector)


##### APPROACH 1: Hybrid cross-fade (Blends a flat history block like a DJ crossfader))
# Dual-Mode Hybrid Cross-Fade Filter (Linear & Exponential Scaling variants)
# Smoothly blends a uniformly averaged history block with the live frame using a velocity-dependent scalar coefficient.
def smart_averaging_fluid_exponential(cath, shot, shotList, numAverages, maxAveragingDistance):
    avgCenterPos = {}
    avgDirVector = {}

    if shot[cath]["valid"]:
        # =========================================================================
        # Smart averaging: average the last N shots, but only valid shots.
        # This function utilizes a nonlinear exponential curve (np.exp) to scale
        # the filter weight. When hand movement initiates, historical data is
        # shed rapidly along a smooth curve to prevent "twitching" or "snapping"
        # artifacts while entirely avoiding structural rubber-band lag.
        # =========================================================================

        # Calculate dynamic smooth-blending weight via an exponential curve
        distanceToLastShot = shot[cath]["distanceToLastShot"]

        # Sensitivity multiplier (e.g., 3.0) controls how aggressively history sheds during motion
        sensitivity = 2.0  # The Blending Split for 3.0: 71% Present / 29% Past; for 2.0: 59% Present / 41% Past
        history_retention = np.exp(-sensitivity * (distanceToLastShot / maxAveragingDistance))
        history_retention = min(max(history_retention, 0.0), 1.0)

        # Alpha acts as the dynamic blending scale between present and past data
        base_alpha = 0.2
        alpha = 1.0 - (1.0 - base_alpha) * history_retention

        # Gather history coordinates without the hard break, keeping only valid frames
        coilPositionsXYZ = {coil: [] for coil in coils}

        for idx in range(1, min(numAverages, len(shotList))):
            if shotList[-idx][cath]["valid"]:
                for coil in coils:
                    coilPositionsXYZ[coil].append(shotList[-idx][cath][coil]["coilPositionXYZ"])

        avgCoilPositionXYZ = {}
        for coil in coils:
            if coilPositionsXYZ[coil]:
                # Calculate simple arithmetic mean of the historical valid shots
                historicalAverage = sum(np.array(p) for p in coilPositionsXYZ[coil]) / len(coilPositionsXYZ[coil])
                # Smoothly blend present and past using the dynamic exponential alpha scale
                avgCoilPositionXYZ[coil] = (alpha * np.array(shot[cath][coil]["coilPositionXYZ"])) + (
                    (1.0 - alpha) * historicalAverage
                )
            else:
                avgCoilPositionXYZ[coil] = np.array(shot[cath][coil]["coilPositionXYZ"])

        # ==========================================================================================================
        # Re-calculate center (reference) positions and direction vectors for slicer matrix based on smart average
        # ==========================================================================================================
        avgCenterPos = (avgCoilPositionXYZ["dist"] + avgCoilPositionXYZ["prox"]) / 2
        avgDirVector = avgCoilPositionXYZ["dist"] - avgCoilPositionXYZ["prox"]
        coilDistance = np.linalg.norm(avgDirVector)
        if coilDistance != 0:
            avgDirVector /= coilDistance

    return (avgCenterPos, avgDirVector)


##### APPROACH 2: "Adaptive EMA"/"Velocity EMA" (Fades every single frame exponentially based on age and hand speed)
# Velocity-Adaptive Exponential Moving Average Filter
# Implements true frame-by-frame exponential smoothing where past data points decay exponentially by age.
# NOTE: does NOT guarantee that 5 valid positions will be collected. Instead, it checks a fixed window of time (numAverages) and simply skips any frames inside that window that are marked invalid.
def smart_averaging_velocity_adaptive_ema(cath, shot, shotList, numAverages, maxAveragingDistance):
    avgCenterPos = {}
    avgDirVector = {}

    if shot[cath]["valid"]:
        # =========================================================================
        # Smart averaging: average the last N shots, but only valid shots.
        # This function implements a true frame-by-frame Velocity-Adaptive EMA.
        # It calculates a base alpha parameter from current catheter displacement.
        # Each individual historical frame is then assigned a progressively decaying
        # weight proportional to its age, compounding at a rate of (1.0 - alpha).
        # When velocity spikes, alpha approaches 1.0, instantly crushing the memory
        # trail of past points to entirely bypass structural rubber-band lag.
        # =========================================================================

        # Calculate dynamic smooth-blending weight via an exponential curve
        distanceToLastShot = shot[cath]["distanceToLastShot"]

        # Sensitivity multiplier (e.g., 2.0) controls how aggressively history sheds during motion
        sensitivity = 2.0
        history_retention = np.exp(-sensitivity * (distanceToLastShot / maxAveragingDistance))
        history_retention = min(max(history_retention, 0.0), 1.0)

        # Alpha acts as the dynamic blending scale between present and past data
        base_alpha = 0.3
        alpha = 1.0 - (1.0 - base_alpha) * history_retention

        # Build true frame-by-frame compounding exponential weights for history
        avgCoilPositionXYZ = {}

        for coil in coils:
            # Start the true EMA sequence with the current live tracking frame
            currentPosition = np.array(shot[cath][coil]["coilPositionXYZ"])

            weighted_sum = alpha * currentPosition
            total_weight = alpha

            # Decay multiplier compounds recursively back through time
            decay_factor = 1.0 - alpha
            current_decay = 1.0

            # Loop through history from newest past frame to oldest past frame
            for idx in range(1, min(numAverages, len(shotList))):
                if shotList[-idx][cath]["valid"]:
                    # CRITICAL FIX: The frame_weight calculation and compounding decay must
                    # only apply when a valid frame is actually present. This ensures that
                    # dropped or invalid frames do not cause artificial, premature memory loss.
                    frame_weight = alpha * current_decay

                    pastPosition = np.array(shotList[-idx][cath][coil]["coilPositionXYZ"])
                    weighted_sum += frame_weight * pastPosition
                    total_weight += frame_weight

                    # Update decay factor for the next encountered valid frame
                    current_decay *= decay_factor

            # Normalize weights to exactly 1.0 to ensure zero spatial drift
            if total_weight > 0:
                avgCoilPositionXYZ[coil] = weighted_sum / total_weight
            else:
                avgCoilPositionXYZ[coil] = currentPosition

        # ==========================================================================================================
        # Re-calculate center (reference) positions and direction vectors for slicer matrix based on smart average
        # ==========================================================================================================
        avgCenterPos = (avgCoilPositionXYZ["dist"] + avgCoilPositionXYZ["prox"]) / 2
        avgDirVector = avgCoilPositionXYZ["dist"] - avgCoilPositionXYZ["prox"]
        coilDistance = np.linalg.norm(avgDirVector)
        if coilDistance != 0:
            avgDirVector /= coilDistance

    return (avgCenterPos, avgDirVector)


##### APPROACH 3: Velocity-Adaptive EMA with Temporal Cap ("Velocity EMA")
# Calculates true frame-by-frame exponential smoothing where past positions decay exponentially by age.
# Dynamically adjusts filtering weight based on hand speed, while imposing a strict 500 ms history lookback limit.
# NOTE: DOES guarantee that up to 5 valid positions will be collected if available. Instead of checking a fixed window of time (numAverages), it actively searches backward until (numAverages) valid points are found, unless it hits the hard 500 ms temporal wall.
def smart_averaging_velocity_adaptive_ema_with_temporal_horizon_cap(cath, shot, shotList, numAverages, maxAveragingDistance):
    avgCenterPos = {}
    avgDirVector = {}

    if shot[cath]["valid"]:
        # =========================================================================
        # Smart averaging: average the last N shots, but only valid shots.
        # This function implements a true frame-by-frame Velocity-Adaptive EMA.
        # It guarantees a robust sample depth by looking backward until it collects
        # up to (numAverages - 1) historical points. To maintain temporal relevance,
        # it implements a strict 500 ms maximum time horizon cap. Assuming a 10 Hz
        # system (100 ms per frame), it will never pull data older than 5 frames ago.
        # =========================================================================

        # Calculate dynamic smooth-blending weight via an exponential curve
        distanceToLastShot = shot[cath]["distanceToLastShot"]

        # Sensitivity multiplier (e.g., 2.0) controls how aggressively history sheds during motion
        sensitivity = 2.0
        history_retention = np.exp(-sensitivity * (distanceToLastShot / maxAveragingDistance))
        history_retention = min(max(history_retention, 0.0), 1.0)

        # Alpha acts as the dynamic blending scale between present and past data
        base_alpha = 0.2
        alpha = 1.0 - (1.0 - base_alpha) * history_retention

        avgCoilPositionXYZ = {}

        for coil in coils:
            # Start the true EMA sequence with the current live tracking frame
            currentPosition = np.array(shot[cath][coil]["coilPositionXYZ"])

            weighted_sum = alpha * currentPosition
            total_weight = alpha

            # Decay multiplier compounds recursively back through time
            decay_factor = 1.0 - alpha
            current_decay = 1.0

            # Tracking variables for unbounded past lookback
            valid_history_found = 0
            idx = 1

            # Define maximum lookback constraints
            frame_rate_hz = 10  # System runs at 10 frames per second
            ms_per_frame = 1000 / frame_rate_hz  # 100 ms per frame step
            max_time_horizon_ms = 500.0  # Rigid 500 ms time horizon wall

            max_frame_lookback = int(max_time_horizon_ms / ms_per_frame)  # Resolves to 5 frames max
            absolute_buffer_limit = min(max_frame_lookback, len(shotList))

            # Search backward until target count is met OR we hit the 500 ms time horizon
            while valid_history_found < (numAverages - 1) and idx <= absolute_buffer_limit:
                if shotList[-idx][cath]["valid"]:
                    valid_history_found += 1

                    # Compounding decay calculation matches frame age order
                    current_decay *= decay_factor
                    frame_weight = alpha * current_decay

                    pastPosition = np.array(shotList[-idx][cath][coil]["coilPositionXYZ"])
                    weighted_sum += frame_weight * pastPosition
                    total_weight += frame_weight

                idx += 1  # Progress to the next oldest step in time

            # Normalize weights to exactly 1.0 to ensure zero spatial drift
            if total_weight > 0:
                avgCoilPositionXYZ[coil] = weighted_sum / total_weight
            else:
                avgCoilPositionXYZ[coil] = currentPosition

        # ==========================================================================================================
        # Re-calculate center (reference) positions and direction vectors for slicer matrix based on smart average
        # ==========================================================================================================
        avgCenterPos = (avgCoilPositionXYZ["dist"] + avgCoilPositionXYZ["prox"]) / 2
        avgDirVector = avgCoilPositionXYZ["dist"] - avgCoilPositionXYZ["prox"]
        coilDistance = np.linalg.norm(avgDirVector)
        if coilDistance != 0:
            avgDirVector /= coilDistance

    return (avgCenterPos, avgDirVector)


def pos_to_matrix(centerPos, dirVector):
    # ===========================================================================================================================================
    # References (rotation matrix)
    # https://math.stackexchange.com/questions/1956699/getting-a-transformation-matrix-from-a-normal-vector
    # Leonhard Euler, "Problema algebraicum ob affectiones prorsus singulares memorabile", Commentatio 407 Indicis Enestoemiani, Novi Comm. Acad. Sci. Petropolitanae 15 (1770), 75-106
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
    matrix[0, 3] = centerPos[0]
    matrix[1, 3] = centerPos[1]
    matrix[2, 3] = centerPos[2]

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
            shotList[idx][cath]["distanceToLastShot"] = np.linalg.norm(
                shotList[idx][cath]["centerPos"] - shotList[idx - 1][cath]["centerPos"]
            )

    for shotIdx in range(1, len(shotList)):
        # Use simulated shotList (up to the current position) from the full shotList.
        # and simulated current shot (last element in array)
        for cath in caths:
            curShot = shotList[shotIdx]
            if curShot[cath]["valid"]:
                validPositionSeen[cath] = True
                [avgCenterPos, avgDirVector] = smart_averaging_IDW(
                    cath,
                    curShot,
                    shotList[0:shotIdx],
                    numAverages,
                    maxAveragingDistance,
                )

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
                transform_message = pyigtl.TransformMessage(
                    pos_to_matrix(avgTipPos, avgDirVector),
                    device_name=str(cath[:1].upper() + cath[1:]) + "_01_TF",
                    timestamp=time(),
                )

                transform_message.header_version = 2
                transform_message.metadata = {
                    "valid": str(curShot[cath]["valid"]),
                    "tipPos": str(avgTipPos.tolist()),
                }

                print(transform_message)
                server.send_message(transform_message)
                sleep(0.05)

    # THIS PREVENTS THE 10054 ERROR UPON RE-RUN
    # ConnectionResetError [WinError 10054] occurs because the old network
    # socket from the previous script execution is still hanging open in the
    # operating system
    print("Safely shutting down pyigtl connections...")
    if "server" in locals() and server is not None:
        import threading

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
        try:
            sleep(0.1)
        except NameError:
            import time

            time.sleep(0.1)
