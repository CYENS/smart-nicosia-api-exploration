"""Living sky and CyNAP ITS infrastructure: live aircraft (adsb.lol), ISS visible passes
(CelesTrak), PWD CCTV and traffic detector inventories (FixCyprus / traffic4cyprus), Swarco
measurement probe, and lightweight GTFS-RT bus position archiving."""

from chatbot.sky import entities as _entities  # noqa: F401 - registers aircraft, its_cctv, its_detector
from chatbot.sky.service import SkyService

__all__ = ["SkyService"]
