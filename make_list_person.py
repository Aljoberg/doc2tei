# makes a listPerson file
# :P

import argparse
import json
import re
import requests
import xml.etree.ElementTree as ET

# whatever this is
QUERY = """
SELECT ?p ?pLabel ?givenLabel ?familyLabel ?birth ?death ?bplaceLabel ?dplaceLabel
       ?sexLabel ?occLabel ?partyLabel ?citizenLabel ?viaf ?gnd ?isni WHERE {
  SERVICE wikibase:mwapi {
    bd:serviceParam wikibase:api "EntitySearch" .
    bd:serviceParam wikibase:endpoint "www.wikidata.org" .
    bd:serviceParam mwapi:search "%s" .
    bd:serviceParam mwapi:language "sl" .
    ?p wikibase:apiOutputItem mwapi:item .
  }
  ?p wdt:P31 wd:Q5 .
  OPTIONAL { ?p wdt:P569 ?birth. }   OPTIONAL { ?p wdt:P570 ?death. }
  OPTIONAL { ?p wdt:P19 ?bplace. }   OPTIONAL { ?p wdt:P20 ?dplace. }
  OPTIONAL { ?p wdt:P735 ?given. }   OPTIONAL { ?p wdt:P734 ?family. }
  OPTIONAL { ?p wdt:P21 ?sex. }      OPTIONAL { ?p wdt:P106 ?occ. }
  OPTIONAL { ?p wdt:P102 ?party. }   OPTIONAL { ?p wdt:P27 ?citizen. }
  OPTIONAL { ?p wdt:P214 ?viaf. }    OPTIONAL { ?p wdt:P227 ?gnd. }
  OPTIONAL { ?p wdt:P213 ?isni. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "sl,en". }
}
"""


def make_list_person(mapping: dict[str, str]):
    for id, whole in mapping.items():
        person = ET.Element("person", {"xml:id": id[1:]})  # strip the hash
        text = whole.strip()

        aff_match = re.search(r"\(([^)]*)\)", text)
        aff = aff_match.group(1).strip() if aff_match else None

        role_match = re.search(r",\s*(.+?)\s*:?\s*$", text)
        minister = role_match.group(1).strip() if role_match else None

        name, surname = (
            "".join(" " + char if char.isupper() else char for char in id[1:])
            .strip()
            .split()
        )

        pers_name = ET.Element("persName")
        surname_elem = ET.Element("surname")
        surname_elem.text = surname
        pers_name.append(surname_elem)
        name_elem = ET.Element("forename")
        name_elem.text = name
        pers_name.append(name_elem)

        person.append(pers_name)

        req = requests.get(
            "https://query.wikidata.org/sparql",
            params={"query": QUERY % f"{name} {surname}"},
            headers={
                "User-Agent": "doc2tei",
                "Accept": "application/sparql-results+json",
            },
        )

        birth_elem = ET.Element("birth")
        death_elem = ET.Element("death")

        bindings = req.json()["results"]["bindings"] if req.ok else []
        if bindings:
            result = bindings[0]  # we're trusting the first one lmao
            for k, v in result.items():
                match (k, v):
                    case ("p", uri):
                        idno = ET.Element("idno", type="URI", subtype="wikidata")
                        idno.text = uri["value"]
                        person.append(idno)
                    case ("birth", birth):
                        birth_elem.attrib["when"] = birth[
                            "value"
                        ]  # value is an iso datetime thing
                    case ("death", death):
                        death_elem.attrib["when"] = death["value"]
                    case ("viaf", viaf):
                        viaf_elem = ET.Element("idno", type="VIAF", subtype="wikidata")
                        viaf_elem.text = viaf["value"]
                        person.append(viaf_elem)
                    case ("isni", isni):
                        isni_elem = ET.Element("idno", type="ISNI", subtype="wikidata")
                        isni_elem.text = isni["value"]
                        person.append(isni_elem)
                    case ("bplaceLabel", birth_place):
                        place_name = ET.Element("placeName")
                        place_name.text = birth_place["value"]
                        birth_elem.append(place_name)
                    case ("dplaceLabel", death_place):
                        place_name = ET.Element("placeName")
                        place_name.text = death_place["value"]
                        death_elem.append(place_name)
                    case ("sexLabel", sex):
                        sex_elem = ET.Element(
                            "sex",
                            value=(
                                "M"
                                if sex["value"] == "moški"
                                else "Ž" if sex["value"] == "ženski" else "unknown"
                            ),
                        )
                        sex_elem.text = sex["value"]
                        person.append(sex_elem)
                    case ("givenLabel", given):
                        if given["value"] != name:
                            # it's not ocr, it's not ocr, SURELY it's not ocr---
                            # ..it was ocr
                            name_elem.text = given["value"]
                    case ("familyLabel", family):
                        if family["value"] != surname:
                            surname_elem.text = family["value"]
                    case ("occLabel", occupation):
                        occ = ET.Element("occupation")
                        occ.text = occupation["value"]
                        person.append(occ)

            if birth_elem.attrib or len(birth_elem):
                person.append(birth_elem)
            if death_elem.attrib or len(death_elem):
                person.append(death_elem)
        else:
            print(f"didn't find {name} {surname}")

        if minister:
            aff_elem = ET.Element("affiliation", role=minister.split()[0].lower())
            role_name = ET.Element("roleName", {"xml:lang": "sl"})
            role_name.text = minister
            aff_elem.append(role_name)
            if aff:
                org = ET.Element("orgName")
                org.text = aff
                aff_elem.append(org)
            person.append(aff_elem)
        elif aff:
            aff_elem = ET.Element("affiliation", role="member")
            org = ET.Element("orgName")
            org.text = aff
            aff_elem.append(org)
            person.append(aff_elem)

        list_person.append(person)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("input", type=str)
    p.add_argument("-o", "--out", type=str, default=None)

    args = p.parse_args()

    list_person = ET.Element(
        "listPerson", {"xml:lang": "sl"}, xmlns="http://www.tei-c.org/ns/1.0"
    )

    govorniki = ET.Element("head", {"xml:lang": "sl"})
    govorniki.text = "Seznam govornikov"
    list_person.append(govorniki)

    speakers = ET.Element("head", {"xml:lang": "en"})
    speakers.text = "List of speakers"
    list_person.append(speakers)

    with open(args.input, encoding="utf-8") as f:
        mapping = json.load(f)
    if isinstance(mapping.get("speakers"), dict):
        mapping = mapping["speakers"]

    make_list_person(mapping)
    xml = ET.tostring(list_person, encoding="utf-8")

    if not args.out:
        print(xml)
    else:
        with open(args.out, "wb") as f:
            f.write(xml)
