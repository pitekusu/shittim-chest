"""Shared keys for the recruitment result projection and its assets."""

import re

GALLERY_RESULT_PREFIX = "RESULT#"
GALLERY_RECORD_TYPE = "student_gacha_gallery"
GALLERY_THUMBNAIL_PREFIX = "student-gacha/thumbnails/v1/"
GALLERY_THUMBNAIL_SIZE = (768, 355)
GALLERY_RESULT_KEY = re.compile(
    r"RESULT#(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z)#([a-f0-9]{32})",
    re.ASCII,
)
