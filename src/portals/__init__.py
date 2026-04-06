from src.portals.naukri import NaukriPortal
from src.portals.indeed import IndeedPortal
from src.portals.foundit import FounditPortal
from src.portals.ziprecruiter import ZipRecruiterPortal
from src.portals.linkedin import LinkedInPortal
from src.portals.glassdoor import GlassdoorPortal

ALL_PORTALS = {
    "naukri": NaukriPortal,
    "indeed": IndeedPortal,
    "foundit": FounditPortal,
    "ziprecruiter": ZipRecruiterPortal,
    "linkedin": LinkedInPortal,
    "glassdoor": GlassdoorPortal,
}

AUTO_APPLY_PORTALS = {"naukri", "indeed", "foundit", "ziprecruiter"}
SCRAPE_ONLY_PORTALS = {"linkedin", "glassdoor"}
