## MRTC (Magnetic Resonance Therapy Control) MR Scanner Interface

MRTC (Magnetic Resonance Therapy Control) is an interface designed to connect Philips MR scanners with external real-time MR-guided treatment devices (such as linear accelerators, focused ultrasound systems, catheters tracking equipment or surgical robotics).

<img width="489" height="367" alt="MRTC" src="https://github.com/user-attachments/assets/3cfa708d-9ef6-4894-8eab-3d67d27c2319" />

### Core Capabilities

* Start an MR examination from an external workstation
* Start and stop scans in a list of scans within an ExamCard
* Update scan geometries during scans
* Receive MR data with low latency to support dynamic image guided treatment
* Clock sync between scanner and workstation
* **Real-Time Guidance:** Enables clinicians (cardiologists/electrophysiologists, oncologists, surgeons) to monitor the treatment and continuously update the target zone in real-time while the patient is on the table.
* **Interoperability:** Facilitates communication between different operating systems and programming languages to allow treatment device manufacturers to customize their specific procedural workflows.

### MRTC Message format
* Messages exchanged between the MR scanner and the therapy control software are transported as byte sequences called Protocol Data Units or PDUs. A PDU consists of a 4-byte length field, followed by an 8-byte protocol version field, followed by a 4-byte message type field, followed by a variable-length message data field, followed by a 4-byte checksum field.
  <img width="535" height="40" alt="MessageFormat" src="https://github.com/user-attachments/assets/0119712c-495f-441f-ae2e-ab5820216fd3" />

* Communication of the message data is implemented via google protobuf, which is available across all platforms (Windows/Linux/Mac) and programming languages (e.g. C, Python)
* All field are encoded as a 32-bit unsigned integer using little-endian byte order
* Length is number of bytes making up the PDU including the 4 bytes of the length field itself
* Protocol version and Message type can be best obtained from the Enum in the .proto file
* Message data is the serialized protobuf message
* A checksum is calculated using the CRC-32c checksum calculation algorithm
* Message data is referred to as payload
* Communication is done using a request followed by a response
* Request message always provides a token
* Response will send the same token to confirm that the response belongs to the request
* Different message types exist, such as control messages from the therapy equipment to the MR scanner (e.g. request the start of a scan), and data messages from the MR scanner to the therapy equipment

  <img width="352" height="157" alt="MessageTypes" src="https://github.com/user-attachments/assets/8cef22cd-af58-4424-8a0d-00d9dc243a22" />


### Practical Impact on Workflows
MRTC is part of the SIGNET project [ITEA SIGNET project](https://itea4.org) as an open, vendor-independent interface. This interface enables real-time coordination between MR scanning, therapy control, and physiological streaming. By eliminating manual, fragmented data tracking steps, validated demonstrators proved major workflow improvements:

* **Oncology Biopsies**: Average procedure times dropped from over 60 minutes to under 20 minutes, while required tissue samples fell from 14 down to just 1 or 2 due to automated precision.
* **Neurology Processing**: Manual data handling and registration steps were cut exactly in half.
* **Cardiac Electrophysiology**: The interface successfully enabled the clinical workflow for in-vivo right-sided atrial flutter ablations, establishing continuous catheter tracking for complex ventricular arrhythmia treatments in the future.
