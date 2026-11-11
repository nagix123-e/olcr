"""Narrow, source-backed ingredient rules; unknown ingredients remain unknown.

Version: 2026-11-07. Frequency alerts are consultation prompts, never diagnoses.
Only explicitly confirmed acute-headache purpose enables these rules.
"""
BASE='https://ichd-3.org/8-headache-attributed-to-a-substance-or-its-withdrawal/8-2-medication-overuse-headache-moh/'
NONOPIOID_SOURCE=BASE+'8-2-3-simple-analgesic-overuse-headache/'
TRIPTAN_SOURCE=BASE+'8-2-2-triptan-overuse-headache/'
# Each key is an explicit generic ingredient, never a guessed product name.
INGREDIENT_CLASSES={
    'アセトアミノフェン':'nonopioid', 'acetaminophen':'nonopioid','paracetamol':'nonopioid',
    'アスピリン':'nonopioid','aspirin':'nonopioid','acetylsalicylic acid':'nonopioid',
    'スマトリプタン':'triptan','sumatriptan':'triptan',
}
# Ingredient classification references in addition to the ICHD-3 subtype pages:
# https://www.nhs.uk/medicines/sumatriptan/
THRESHOLDS={'nonopioid':15,'triptan':10}
PURPOSES={'頭痛時','頭痛の頓服','頭痛の急性期治療','acute headache'}
