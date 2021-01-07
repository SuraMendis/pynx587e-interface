# Standard library imports
import queue
import time
from threading import Thread

# Related third party imports.
import serial

# Local application imports
from pynx587e.serialreader import Serialreader
from pynx587e.flexdevice import FlexDevice

class PanelInterfaceError(Exception):
    '''Basic Exception for errors raised with PanelInterface'''

class KeyMapError(PanelInterfaceError):
    ''' Keymap should be US or AUNZ '''
    
class PanelInterface:
    ''' Connect and manage Interlogix, Caddx and Hills Reliance alarm
    panels via the NX-587E serial module.
    
    :param port: An NX148E function command or user code
    :type port: int
    :param max_zone: Highest Zone number to track
    :type max_zone: int
    :param max_partitions: Highest Partition number to track
    :type max_partitions: int
    :param keymap: USA or AUNZ (Hills Reliance panels should use AUNZ)
    :type max_partitions: string
    :param cb: function in your application that gets called when a
     Zone or Partition Event occurs.
    :type cb: function

    :raises pynx587e.controller.KeyMapError: keymap must be USA or AUNZ
    '''
    def __init__(self, port, max_zone,max_partitions, keymap,cb):
        # A Zone Status Message syntax is like: ZN002FttBaillb where:
        #  ZN = Zone Identifer
        #  002 = The Zone Number/ID
        #  FttBaillb = each character relates to the definition
        #  (upper case true; else false)
        #  in the _ZONE_ELEMENTS list below (order is important)
        self._ZONE_ELEMENTS = [
            'fault',
            'tamper',
            'trouble',
            'bypass',
            'alarmMemory',
            'inhibit',
            'lowBattery',
            'lost',
            'memoryBypass',
        ]

        # A Partition Status Message syntax is like: PA1RasCeEps where:
        #  PN = Partition Identifer
        #  1 = The Partition Number/ID
        #  RasCeEps = each character relates to the definition 
        #  (upper case true; else false) in the _ZONE_ELEMENTS list
        #  below (order is important)
        self._PARTITION_ELEMENTS = [
            'ready',
            'armed',
            'stay',
            'chime',
            'entryDelay',
            'exitPeriod',
            'previousAlarm',
            'siren'
        ]

        # _NX_MESSAGE_TYPES is a dictionary that defines supported
        # message types. The key is the message type and the value
        # is the previously defined elements (e.g _PARTITION_ELEMENTS)
        self._NX_MESSAGE_TYPES={
            "ZN":self._ZONE_ELEMENTS,
            "PA":self._PARTITION_ELEMENTS
        }   

        # Define the number of devices in the alarm system
        # e.g "ZN":48 means the highest Zone number is 48
        self._NX_MAX_DEVICES={
            "ZN":max_zone,
            "PA":max_partitions,
        }

        # Serial port the NX587E is attached to
        # Windows e.g: COM1
        # Linux e.g /dev/ttyUSB0
        self._port = port

        # The callback function called when a partition or zone
        # stauts changes
        self.callbackf = cb

        # Configuration string for NX-587E
        # This value is lost if the NX-587E loses power from the
        # alarm panel.
        # TODO: START_UP_OPTIONS should be periodically sent to
        #       the NX587E to mitigate effect of power loss
        START_UP_OPTIONS='taliPZn'

        # Set instance variable keymap or throw exception
        # keymap is used by panel_command(...)
        if (keymap != "USA" and keymap !="AUNZ"):
            raise KeyMapError("keymap must be: USA or AUNZ")
        else:
            self._keymap = keymap

        # Disconnection flag
        self._run_flag = True

        # Queues for thread communication
        self._command_q = queue.Queue(maxsize=0)
        self._raw_event_q = queue.Queue(maxsize=0)
        self._consumer_q = queue.Queue(maxsize=0)
        
        # Create deviceBank from NX_MAX_DEVICES definition to represent
        # the defined number of devices (e.g. Zones and Partitions)
        self.deviceBank = {}
        for device, max_item in self._NX_MAX_DEVICES.items():
            self.deviceBank[device] = []
            i = 0
            while i < max_item:
                self.deviceBank[device].append(FlexDevice(self._NX_MESSAGE_TYPES[device]))
                self._direct_query(device,i+1)
                #time.sleep(0.05)
                i = i+1

        # NOTE: Thread creation happens in _control
        self._control()
        self._configure_nx587e(START_UP_OPTIONS)


    def _configure_nx587e(self, options):
        # Add NX587E configuration options to the command queue
        # execution. Typically called during NX587E instantiation 
    
        try:
            self._command_q.put_nowait(options)
        except serial.SerialException as e:
            print(e)
        # Give some time for the _serial_writer thread to process
        time.sleep(0.25)
    

    def _process_event(self,raw_event):
        ''' Decode, track and report changes to transition
        messages (ZN and PA) and their individual elements.

        .. note: If the existing element value is -1 then this
        is the first update to the element (typically during 
        instantiation of this module) and the callback function 
        is skipped.

        :param raw_event: A Zone or Partition Status Message
        :type raw_event: string
        '''
        # Determine if raw_event is a valid status message type by
        # comparing it with in NX_MESSAGE_TYPES.
        for key_nxMsgtypes in self._NX_MESSAGE_TYPES:
            if raw_event[0:2] == key_nxMsgtypes:
                # Determine the device ID from raw_event. ID can be
                # 3 chars (001 for Zone Status Messages: ZN001) or 1
                # char (1 for Partition Status Messages PA1)                
                #
                # Begining from the 3rd (indexed from 0) character of
                # raw_event, check if the char is numeric and expand
                # the range until a non-numeric is found. id will now
                # contain the required id.
                id_start_char = 2
                num_char= id_start_char + 1
                while raw_event[2:num_char].isnumeric() == True:
                        id = int(raw_event[2:num_char])
                        num_char += 1
                        # raw_event can contain a 3 digit or 1 digit
                        # id so the position of non-id message
                        # attributes is offset due to the length of 
                        # the id in raw_event. id_start_char tracks 
                        # the start position of non-id characters 
                        id_start_char += 1

                # Construct a dictionary to represent the status
                # characters contained in raw_event positioned after
                # the id. UPPER CASE characters represent 'TRUE',
                # lower case characters represent 'False'. 
                # The character position in raw_event message 
                # determins the underlying attribute/property as
                # defined in NX_MESSAGE_TYPES.
                NXMessage = {}
                for i, v in enumerate(raw_event[id_start_char:len(raw_event)-1]):
                    NXMessage[self._NX_MESSAGE_TYPES[key_nxMsgtypes][i]] = v.isupper()
                
                # The attribute characters of raw_event is now 
                # represented in NXMessage (excluding message 
                # type and id).
                #
                # Iterate through the current message represented in
                # NXMessage items and compare each attribute with 
                # that of previous attribute value stored in
                # deviceBank list. 
                # 
                # NOTE: deviceBank list stores the previous state
                # positioned by the sequential device id as the index.
                # Therefore, ensure the id is within the NX_MAX_DEVICES
                # value to avoid a out of range index error

                if id <= self._NX_MAX_DEVICES[key_nxMsgtypes]:
                    # id is within range
                    for msg_key, msg_value in NXMessage.items():
                        # Get the previous attribute value and compare
                        # current value. If it doesn't match, an 'event'
                        # has occurred, so update the state with the new
                        # value
                        previous_attribute_value = self.deviceBank[
                            key_nxMsgtypes][id-1].get(msg_key)
                        
                        skip_callback = False
                        if previous_attribute_value != msg_value:
                            if previous_attribute_value == -1:
                                skip_callback=True
                            else: 
                                pass
                            
                            # Update value    
                            self.deviceBank[key_nxMsgtypes][id-1].set(msg_key, msg_value)

                            # Construct an event dictionary to
                            # represent the latest state
                            event = {"event":key_nxMsgtypes,
                                    "id":id,
                                    "tag":msg_key,
                                    "value":msg_value,
                                    "time": self.deviceBank[key_nxMsgtypes][id-1].get(str(msg_key+'_time'))
                                    }
                            # Execute the callback function with the 
                            # latest event state that changed.
                            if skip_callback == False:
                                self.callbackf(event)
                        else:
                            # Message not supported
                            pass
                else:
                    # Received a message with an ID > MAX devices, ignore message
                    pass

    def getStatus(self,query_type,id,element):
        ''' Returns the individual status and time
        for a defined element as as list.

        :param query_type: Query type as defined in _NX_MESSAGE_TYPES
        :type query_type: string
        
        .. note:: Supported elements are defined in _NX_MESSAGE_TYPES
           For example: getStatus('ZN',1,fault) could return
           [true,2021-01-05 16:00:29.689725] which means:
            - status of Zone 1's fault (tripped) is TRUE;
            - and the associated event time.

        :return: List [-1,-1] for invalid requests
        :rtype: List
        '''
        # Check if the query_type is valid as defined in
        # _NX_MESSAGE_TYPES
        invalid_status = ['-1','-1']

        if query_type in self._NX_MESSAGE_TYPES:
            # Check if the id is valid as defined in _NX_MAX_DEVICES
            if id <= self._NX_MAX_DEVICES[query_type]:
                cached_attribute=self.deviceBank[query_type][id-1].get(element)
                cached_attribute_time=self.deviceBank[query_type][id-1].get(element+'_time')
                status = [cached_attribute,cached_attribute_time]
            else:
                status = invalid_status
        else:
            status = invalid_status
            
        return status


    def _direct_query(self,query_type,id):
        '''Directly query the Zone or Partition status from the
        NX587E. Results are processed by _event_process. 

        :param query_type: Query type as defined in _MX_MESSAGE_TYPES
        :type query_type: string

        :raises serial.SerialException: If serial port error occurs

        .. note:: _direct_query is for internal use module use. Users of 
        pyNX587E should use getStatus rather than _direct_query.

        .. note:: _event_process inhibits its callback function for the 
        first status response it processes. This allows _direct_query
        to be used internally to establish an accurate state during 
        start-up.
        '''
        # Check if the query_type is valid as defined in
        # _NX_MESSAGE_TYPES
        if query_type in self._NX_MESSAGE_TYPES:
            # Check if the id is valid as defined in _NX_MAX_DEVICES
            if id <= self._NX_MAX_DEVICES[query_type]:
                # Construct a query based on the NX587E Specification
                # Q001 to Q192 is for Zone Queries (Zone 1-192)
                # Q193 to Q200 is for Partition  Queries (1-9)
                if query_type == "PA":
                    query="Q"+str(192+id)
                elif query_type=="ZN":
                    query="Q"+str(id).zfill(3)
                # Put the query into the _command_q
                # which will be processed by the serial writer thread
                try:
                    self._command_q.put_nowait(query)
                except serial.SerialException as e:
                    print(e)

    def panel_command(self, in_command):
        ''''Sends an alarm panel command or user code via the NX587E 
        interface. 

        :param in_command: An NX148E function command or user code
        :type in_command: string

        :raises serial.SerialException: If serial port error occurs

        .. note::
           AU/NZ installations support the following commands
           partial, chime, exit, bypass, on, fire, medical, hold_up,
           or a 4 or 6 digit user code

        .. note::
           Non-AU/NZ installations support the following commands
           stay, chime, exit, bypass, cancel, fire, medical, hold_up,
           or a 4 or 6 digit user code.
        '''
        # The NX587E presents as a NX148E (Non-AU/NZ keypad version)
        #
        # Australian/NZ alarm panels (e.g. Hills Reliance) expects 
        # a NX148E (AU/NZ Version) and not the version presented by 
        # the NX587E.
        #   
        # Consequently, the self.keymap parameter must be set to 2 for
        # AU/NZ installations; or 1 for non-AU/NZ installations.
        if self._keymap == "AUNZ":
            supported_commands = {
                "partial":"K", # Sending K does a partial/stay arm
                "chime":"C",
                "exit":"E",
                "bypass":"B",
                "on":"S", # Sending 'S' quick-arm 
                "fire":"F",
                "medical":"M",
                "hold_up":"H",}
        else:
            # The NX587E default supported keymap
            supported_commands = {
                "stay":"S",
                "chime":"C",
                "exit":"E",
                "bypass":"B",
                "cancel":"K",
                "fire":"F",
                "medical":"M",
                "hold_up":"H",}
            
       
        # A 4 or 6 digit code is also a valid input
        # This typically arms/disarms the panel
        if in_command.isnumeric() and (
            len(in_command) == 4 or len(in_command == 6)):
            command = in_command
        elif in_command in supported_commands:
            command = supported_commands[in_command]
        
        # Send the command to the _command_q Queue
        if command != "":
            try:
                 self._command_q.put_nowait(command)
            except serial.SerialException as e:
                print(e)

    def _serial_writer(self,serial_conn,command_q):
        ''' Reads command from queue and writes to the serial port.
        
        :param serial_conn: An instance of serial.Serial from 
        pySerial.
        :type serial_conn: serial.Serial

        :param command_q: Queue to read commands from
        :type command_q: Queue

        :raises queue.Empty if queue is empty and tries again

        .. note:: Designed to run as a daemonic thread
        '''
        while True:
            try:
                # ensure a blocking mechanism is used to reduce CPU
                # usage i.e do not use get_no_wait()
                command = command_q.get()
            except queue.Empty:
                pass
            else:
                b = bytearray()
                b.extend(command.encode())
                serial_conn.write(b)

    def _serial_reader(self,serial_conn,raw_event_q):
        ''' Reads message from serial port and writes it to a Queue
        for further processing.
        
        :param serial_conn: An instance of serial.Serial from 
        pySerial.
        :type serial_conn: serial.Serial

        :param raw_event_q: Queue to write serial message to
        :type command_q: Queue

        .. note:: Designed to run as a daemonic thread
        '''
        # seralreader is wrapper for pyserial that provides a 
        # higher-performance readline function
        # DO NOT use read_until or readline from the pyserial 
        serial_reader = Serialreader(serial_conn)

        while True:
            # NX587E outputs an event starting with a line feed and 
            # terminating with a charater break
            try:
                raw_line = serial_reader.readline().decode().strip()
            except serial.SerialException:
                pass
                # manage a hot-unplug here
            else:
                if (raw_line):
                    raw_event_q.put(raw_line)

    def _event_producer(self,serial_conn, raw_event_q, consumer_q):
        while self._run_flag:
            time.sleep(0.01)
            try:
                raw_event = raw_event_q.get_nowait()
            except queue.Empty:
                pass
            else:
                # process the raw event
                self._process_event(raw_event)
                

    def _control(self):
        try:
            serial_conn = serial.Serial(port=self._port)
        except serial.SerialException as e:
            print(e)
        else:
            # Threads
            serial_writer_thread = Thread(
                target=self._serial_writer,
                args=(serial_conn,
                      self._command_q,
                     ),
                daemon=True
                )

            serial_reader_thread = Thread(
                target=self._serial_reader,
                args=(serial_conn,
                      self._raw_event_q,
                     ),
                daemon=True
                )

            event_producer_thread = Thread(
                target=self._event_producer,
                args=(serial_conn,
                      self._raw_event_q,
                      self._consumer_q,
                     ),
                )

            # Start threads
            serial_writer_thread.start()
            serial_reader_thread.start()
            event_producer_thread.start()
   

    def stop(self):
        self._run_flag = False