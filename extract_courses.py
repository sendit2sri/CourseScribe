#!/usr/bin/env python3
"""
Extract course data from Temenos TLC pathway HTML into structured JSON.

Parses the scSlider tab/pathway/course hierarchy from the TLC portal HTML
and outputs a JSON file with all categories, pathways, and courses.

Usage:
    python extract_courses.py <input.html> [--output courses.json] [--pretty]
"""

import argparse
import json
import re
import sys

from bs4 import BeautifulSoup


def parse_pathway_heading(heading_text: str) -> dict:
    """Parse a pathway heading string into structured data.

    Examples:
        "Temenos Transact Business Accredited I 17 Courses I 4 Exam I 160 Credits"
        "Temenos Transact Technical Migration Innovator | 40 Courses | 16 Exams | 680 Credits"
    """
    text = heading_text.strip()
    # Split on ' I ' or ' | ' delimiter
    parts = re.split(r'\s+[I|]\s+', text)

    title = parts[0].strip() if parts else text
    course_count = 0
    exam_count = 0
    credits = 0

    for part in parts[1:]:
        part = part.strip()
        match_courses = re.match(r'(\d+)\s+Courses?', part, re.IGNORECASE)
        match_exams = re.match(r'(\d+)\s+Exams?', part, re.IGNORECASE)
        match_credits = re.match(r'(\d+)\s+Credits?', part, re.IGNORECASE)

        if match_courses:
            course_count = int(match_courses.group(1))
        elif match_exams:
            exam_count = int(match_exams.group(1))
        elif match_credits:
            credits = int(match_credits.group(1))

    return {
        "title": title,
        "course_count": course_count,
        "exam_count": exam_count,
        "credits": credits,
    }


def extract_course_code(name: str) -> tuple[str, str]:
    """Extract a course code from the end of a course name.

    Course codes follow patterns like: TE1TRTIM, TR1PROVE, FC1PRFCM1, MB1USCOR1,
    AN1PRPOV, IF3PRMRB, IN1PRESS, TD3PRESS1, QU1PRB90, JO2MAFOB1

    Returns (clean_name, code) or (original_name, "") if no code found.
    """
    # Match codes like: 2-4 uppercase letters, 1 digit, 2-5 uppercase letters, optional digits
    # Also handles codes like MB1USAFR1, AN1PRIOA, IN3QUQPI1
    pattern = r'\s+([A-Z]{2,4}\d[A-Z]{2,5}\d*)\s*$'
    match = re.search(pattern, name.strip())
    if match:
        code = match.group(1)
        clean_name = name[:match.start()].strip()
        return clean_name, code

    # Try with trailing whitespace variants or revision suffixes stripped
    # Some names have " - R23 Revision 1 English" appended
    cleaned = re.sub(r'\s*-\s*R\d+\s+Revision\s+\d+\s+English\s*$', '', name.strip())
    match = re.search(pattern, cleaned)
    if match:
        code = match.group(1)
        clean_name = cleaned[:match.start()].strip()
        return clean_name, code

    return name.strip(), ""


def extract_cell_text(td) -> str:
    """Extract clean text from a table cell, handling nested divs and links."""
    text = td.get_text(separator=' ', strip=True)
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text).strip()
    # Remove non-breaking spaces
    text = text.replace('\xa0', ' ').strip()
    return text


def extract_course_url(td) -> str:
    """Extract the first course URL from a table cell."""
    link = td.find('a', href=True)
    if link:
        return link['href']
    return ""


def parse_credits(text: str) -> int:
    """Parse a credits value, handling combined values like '40 | 40'."""
    text = text.strip()
    if not text:
        return 0
    # Handle "40 | 40" format - sum them
    if '|' in text:
        total = 0
        for part in text.split('|'):
            part = part.strip()
            match = re.search(r'(\d+)', part)
            if match:
                total += int(match.group(1))
        return total
    match = re.search(r'(\d+)', text)
    return int(match.group(1)) if match else 0


def parse_dependencies(text: str) -> list[str]:
    """Parse dependency text into a list of course codes."""
    text = text.strip()
    if not text:
        return []
    # Split on commas or newlines, filter empty
    deps = re.split(r'[,\n]+', text)
    return [d.strip() for d in deps if d.strip()]


def extract_courses_from_table(table) -> list[dict]:
    """Extract all courses from a pathway table."""
    courses = []
    rows = table.find_all('tr')

    for row in rows:
        # Skip header rows
        if row.find('th'):
            continue

        cells = row.find_all('td')
        if len(cells) < 5:
            continue

        # Column 0: Course name + URL
        raw_name = extract_cell_text(cells[0])
        url = extract_course_url(cells[0])
        name, code = extract_course_code(raw_name)

        # Column 1: Status
        status = extract_cell_text(cells[1])

        # Column 2: Exam
        exam = extract_cell_text(cells[2])

        # Column 3: Credits
        credits = parse_credits(extract_cell_text(cells[3]))

        # Column 4: Dependencies
        dependencies = parse_dependencies(extract_cell_text(cells[4]))

        if not name:
            continue

        courses.append({
            "name": name,
            "code": code,
            "status": status,
            "exam": exam,
            "credits": credits,
            "dependencies": dependencies,
            "url": url,
        })

    return courses


def extract_all(html: str) -> dict:
    """Parse the full HTML and extract all categories, pathways, and courses."""
    soup = BeautifulSoup(html, 'html.parser')
    categories = []

    # Find all tab labels to get category titles
    tab_labels = soup.find_all('label', class_='tooltiplbl')

    for label in tab_labels:
        tab_input_id = label.get('for', '')  # e.g., "tab2"
        if not tab_input_id:
            continue

        tab_num_match = re.search(r'(\d+)', tab_input_id)
        if not tab_num_match:
            continue
        tab_num = int(tab_num_match.group(1))

        # Get title from span
        title_span = label.find('span', id=f'tabTitle{tab_num}')
        title = title_span.get_text(strip=True) if title_span else label.get('title', '')

        # Find the corresponding tab content
        tab_content = soup.find('div', id=f'tabContent{tab_num}')
        if not tab_content:
            continue

        # Find all pathway checkboxes within this tab content
        pathway_inputs = tab_content.find_all('input', class_='pathway-checkbox')
        pathways = []

        for pathway_input in pathway_inputs:
            pathway_id = pathway_input.get('id', '')  # e.g., "pathway-1-1"

            # The heading and table are inside the next label sibling
            label_el = pathway_input.find_next_sibling('label')
            if not label_el:
                continue

            # Get pathway heading
            heading_el = label_el.find('p', class_='pathway-heading')
            if not heading_el:
                continue

            heading_data = parse_pathway_heading(heading_el.get_text(strip=True))

            # Get courses from the table
            table = label_el.find('table', class_='pathway_table_style')
            courses = extract_courses_from_table(table) if table else []

            pathways.append({
                "pathway_id": pathway_id,
                "title": heading_data["title"],
                "course_count": heading_data["course_count"],
                "exam_count": heading_data["exam_count"],
                "credits": heading_data["credits"],
                "courses": courses,
            })

        categories.append({
            "tab_id": tab_num,
            "title": title,
            "pathways": pathways,
        })

    return {"categories": categories}


def main():
    parser = argparse.ArgumentParser(
        description="Extract Temenos TLC course data from HTML to JSON"
    )
    parser.add_argument("input", help="Path to the HTML file to parse")
    parser.add_argument(
        "--output", "-o",
        help="Output JSON file path (defaults to stdout)",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print the JSON output",
    )
    args = parser.parse_args()

    with open(args.input, 'r', encoding='utf-8') as f:
        html = f.read()

    result = extract_all(html)

    indent = 2 if args.pretty else None
    json_str = json.dumps(result, indent=indent, ensure_ascii=False)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(json_str)
            f.write('\n')
        # Print summary to stderr
        total_pathways = sum(len(c["pathways"]) for c in result["categories"])
        total_courses = sum(
            len(p["courses"])
            for c in result["categories"]
            for p in c["pathways"]
        )
        print(
            f"Extracted {len(result['categories'])} categories, "
            f"{total_pathways} pathways, {total_courses} courses "
            f"→ {args.output}",
            file=sys.stderr,
        )
    else:
        print(json_str)


if __name__ == "__main__":
    main()
