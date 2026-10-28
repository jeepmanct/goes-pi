"""Run: python3 tests/test_tropical.py. NHC outlook and advisory parsing on sample text."""
import pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import tropical

none = "Tropical Weather Outlook\nNWS National Hurricane Center Miami FL\n200 PM EDT Wed Oct 28 2026\n\nFor the North Atlantic...Caribbean Sea and the Gulf of America:\n\nTropical cyclone formation is not expected during the next 7 days.\n\n$$\nForecaster Reinhart\n"
o = tropical.parse_outlook(none)
assert o["none"] and not o["areas"] and o["issued"].startswith("200 PM EDT")
busy = none.replace("Tropical cyclone formation is not expected during the next 7 days.",
    "Southwestern Caribbean Sea:\nA broad area of low pressure is producing disorganized showers.\n* Formation chance through 48 hours...medium...50 percent.\n* Formation chance through 7 days...high...70 percent.")
o = tropical.parse_outlook(busy)
assert not o["none"] and o["areas"][0]["two_day"] == 50 and o["areas"][0]["seven_day"] == 70, o
adv = """BULLETIN
Hurricane Example Advisory Number  12
NWS National Hurricane Center Miami FL
500 PM EDT Wed Oct 28 2026

SUMMARY OF 500 PM EDT...2100 UTC...INFORMATION
-----------------------------------------------
LOCATION...23.1N 80.2W
MAXIMUM SUSTAINED WINDS...90 MPH...150 KM/H
PRESENT MOVEMENT...NNE OR 20 DEGREES AT 12 MPH...19 KM/H
MINIMUM CENTRAL PRESSURE...975 MB...28.79 INCHES

WATCHES AND WARNINGS
"""
a = tropical.parse_advisory(adv)
assert a["title"] == "Hurricane Example" and a["number"] == "12", a
assert a["keys"]["Maximum Sustained Winds"].startswith("90 MPH") and a["keys"]["Location"] == "23.1N 80.2W", a
assert tropical.in_season(time.mktime((2026, 10, 28, 12, 0, 0, 0, 0, -1))) and not tropical.in_season(time.mktime((2026, 12, 1, 12, 0, 0, 0, 0, -1)))
print("ok")
