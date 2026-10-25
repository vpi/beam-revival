"""The Beam+ "brain": perception and behaviour that run off-robot (face recognition, greetings).

It connects to the robot's motor server like a driving page does, receives camera frames,
and sends back recognised faces, speech and (later) drive commands. Driving from the brain is
always lower priority than a person on a driving page.
"""
