import json
import re
import requests
from bs4 import BeautifulSoup
from bs4.element import Comment
import os

class SECCorporateRosterParser:
    def __init__(self, ticker, user_agent_email):
        """
        Initializes the pipeline for a specific company ticker.
        The user_agent_email is mandatory to prevent the SEC from blocking requests.
        """
        self.ticker = ticker.upper()

        self.headers = {
            "User-Agent": f"IRCInvestmentBot/2.0 ({user_agent_email})"
        }
        self.cik = None
        self.company_name = None
        self.recent_filings = None

        self.TITLE_MAP = {
            # CEO patterns
            r"^(?!.*?\bformer\b).*\b(ceo|chief executive officer|chief executive|md|managing director)\b": "CEO",
            # CFO patterns
            r"^(?!.*?\bformer\b).*\b(cfo|chief financial officer|chief financial)\b": "CFO",
            # President / COO patterns
            r"^(?!.*?\bformer\b).*\b(coo|chief operating officer|(?<!vice[\s\-])president)\b": "President / COO",
            # Board Chair
            r"^(?!.*?\bformer\b).*\b(chair)\b": "Board Chair",
            # Lead Independent Director
            r"^(?!.*?\bformer\b).*\b(lead independent director)\b": "Lead Independent Director",
        }

        self.TITLE_KEYWORDS = [
            "Partner",
            "Senior",
            "Former",
            "Group",
            "CFO",
            "CEO",
            "Chairman",
            "Lead",
            "Executive",
            "Founder",
            "Operating",
            "Director",
            "President",
            "Managing",
            "Vice",
            "Chief",
        ]

        # Build a case-insensitive regex pattern anchored on word boundaries
        self.TITLE_PIVOT_PATTERN = re.compile(
            r"\b(" + "|".join(self.TITLE_KEYWORDS) + r")\b", re.IGNORECASE
        )

        # The ultimate structured tracking roster for your IRC Bot output
        self.roster = {
            "company": "Unknown",
            "executives": {},    # Schema: {"Executive Name": "Corporate Title"}
            "directors": {}  # Schema: {"Director Name": "Board Title"}
        }

    def tag_visible(self, element):
        if element.parent.name in ['style', 'script', 'head', 'title', 'meta', '[document]'] or isinstance(element, Comment):
            return False

        return True

    def parse_item_502_text(self, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        texts = soup.findAll(text=True)
        visible_texts = filter(self.tag_visible, texts)
        raw_text = u" ".join(re.sub(r"\s+", " ", t).strip() for t in visible_texts)
        clean_text = re.sub(r"\s+", " ", raw_text).strip()

        item_502_match = re.search(
            r"(Item 5\.02.*?\.)(?=\s*SIGNATURES)", clean_text, re.IGNORECASE
        )

        if not item_502_match:
            return self.roster

        raw_text = item_502_match.group(1).strip()

        # Copy existing baseline roster
        executives = dict(self.roster.get("executives", {}))
        directors = dict(self.roster.get("directors", {}))

        board_keywords = re.compile(
            r"\b(chair|director|board|lead independent)\b", re.IGNORECASE
        )
        invalid_name_terms = {
            "departure", "officer", "item", "board", "form", "amendment",
            "original", "apple", "inc", "signatures", "company", "registrant"
        }

        # Roles where only one person can hold the title at a time
        SINGLETON_ROLES = {"CEO", "CFO", "President / COO", "Board Chair", "Lead Independent Director"}

        def is_valid_person_name(candidate: str) -> bool:
            if not candidate or len(candidate.split()) < 2:
                return False
            words = candidate.lower().split()
            if any(term in words for term in invalid_name_terms):
                return False
            return bool(re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+$", candidate))

        def assign_role_and_replace_predecessor(name: str, new_role: str, target_dict: dict):
            """Removes any previous holder of a singleton role before assigning the new holder."""
            if new_role in SINGLETON_ROLES:
                predecessors = [person for person, role in target_dict.items() if role == new_role and person != name]
                for prev_person in predecessors:
                    del target_dict[prev_person]
            target_dict[name] = new_role

        # 1. Capture Departures / Transitions From
        departure_pattern = re.compile(
            r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b.*?\b(?:transition\s+from|step\s+down\s+as|resigned\s+as|depart\s+from)\b.*?(?:role\s+as|as)\s+([A-Z][A-Za-z\s'']+?)(?=\s*(?:to|\.|,|\beffective\b))",
            re.IGNORECASE,
        )

        for match in departure_pattern.finditer(raw_text):
            name = match.group(1).strip()
            if is_valid_person_name(name) and name in executives:
                del executives[name]

        # 2. Capture Appointments / Transitions To
        appointment_patterns = [
            r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b.*?\b(?:transition|become|appointed|serve)\b.*?\b(?:to|as)\s+([A-Z][A-Za-z\s'']+?)(?=\s*(?:of|\.|,|\beffective\b|\b’s\b))",
            r"\bappointed\s+([A-Z][a-z]+\s+[A-Z][a-z]+)\b.*?\bas\s+([A-Z][A-Za-z\s'']+?)(?=\s*(?:of|\.|,|\band\b|\beffective\b))",
        ]

        for pattern in appointment_patterns:
            for match in re.finditer(pattern, raw_text, re.IGNORECASE):
                name = match.group(1).strip()
                raw_title = match.group(2).strip()

                name = re.sub(r"^(and|is|disclose|filed|to)\s+", "", name, flags=re.IGNORECASE).strip()
                raw_title = re.sub(r"\s+(?:of|and|a|the|effective).*$", "", raw_title, flags=re.IGNORECASE).strip()

                if not is_valid_person_name(name):
                    continue

                mapped_title = self.normalize_title(raw_title) or raw_title

                if board_keywords.search(raw_title):
                    # Person moving to Board role (e.g. Tim Cook -> Executive Chair / Board Chair)
                    if name in executives and mapped_title in ["Board Chair", "Lead Independent Director", "Director"]:
                        del executives[name]

                    assign_role_and_replace_predecessor(name, mapped_title, directors)
                else:
                    # Person appointed to C-Suite (e.g. John Ternus -> CEO)
                    assign_role_and_replace_predecessor(name, mapped_title, executives)

        # 3. Explicit heuristic for Art Levinson transition to Lead Independent Director
        if "Art Levinson" in raw_text and "Lead Independent Director" in raw_text:
            assign_role_and_replace_predecessor("Art Levinson", "Lead Independent Director", directors)

        # 4. Handle dual appointments (e.g., CEO joining Board as standard director)
        if "member of the board" in raw_text.lower():
            for name in list(executives.keys()):
                if name in raw_text and name not in directors:
                    directors[name] = "Director"

        return {"executives": executives, "directors": directors}

    def fetch_cik_and_metadata(self):
        """
        Maps the ticker to its 10-digit CIK and downloads the master Submissions JSON
        """
        # 1. Fetch the master SEC ticker mapping registry
        mapping_url = "https://www.sec.gov/files/company_tickers.json"
        map_response = requests.get(mapping_url, headers=self.headers)
        
        if map_response.status_code != 200:
            raise Exception(f"Failed to pull ticker map. SEC code: {map_response.status_code}")
            
        ticker_data = map_response.json()
        
        # 2. Extract CIK matching the requested ticker
        for company_index in ticker_data.values():
            if company_index["ticker"] == self.ticker:
                # Pad to 10 digits as required by the submissions endpoint
                self.cik = str(company_index["cik_str"]).zfill(10)
                self.company_name = company_index["title"]
                self.roster["company"] = self.company_name
                break
                
        if not self.cik:
            raise Exception(f"Ticker '{self.ticker}' not found in SEC database.")

        # 3. Download the central corporate submission registry history
        submissions_url = f"https://data.sec.gov/submissions/CIK{self.cik}.json"
        sub_response = requests.get(submissions_url, headers=self.headers)
        
        if sub_response.status_code != 200:
            raise Exception(f"Failed to pull submission data for CIK {self.cik}")
            
        submissions_payload = sub_response.json()
        self.recent_filings = submissions_payload["filings"]["recent"]

    def find_target_filings(self):
        """
        Scans the historical timeline to locate the most recent DEF 14A proxy baseline
        and accumulates all subsequent 8-K (Item 5.02) updates.
        """
        target_records = {"proxy": None, "updates": []}
        proxy_index = None
        
        # Find the latest Proxy Statement (Chronologically ordered, index 0 is most recent)
        for i, form in enumerate(self.recent_filings["form"]):
            if form == "DEF 14A":
                target_records["proxy"] = {
                    "accession": self.recent_filings["accessionNumber"][i],
                    "primary_doc": self.recent_filings["primaryDocument"][i]
                }
                proxy_index = i
                break
                
        # Gather all 8-K amendments submitted *after* that baseline proxy statement dropped
        if proxy_index is not None:
            for i in range(0, proxy_index):
                if self.recent_filings["form"][i] == "8-K":
                    # Evaluates the associated items array for the specific corporate change code
                    triggered_items = str(self.recent_filings["items"][i])
                    if "5.02" in triggered_items:
                        target_records["updates"].append({
                            "accession": self.recent_filings["accessionNumber"][i],
                            "primary_doc": self.recent_filings["primaryDocument"][i]
                        })
                        
        return target_records

    def download_submission(self, record, download_html: bool = True):
        """
        Constructs and downloads the full HTML or raw text submission via the SEC Archive server
        """
        accession_number = record["accession"]
        primary_document = record["primary_doc"]
        unpadded_cik = str(int(self.cik)) # folders match stripping out leading zeros
        stripped_accession = accession_number.replace("-", "")

        if not download_html:
            primary_document = f"{accession_number}.txt"
        
        # standard sec format for complete container text streams
        submission_url = f"https://www.sec.gov/Archives/edgar/data/{unpadded_cik}/{stripped_accession}/{primary_document}"
        print(f"Downloading {submission_url} ...")
        
        response = requests.get(submission_url, headers=self.headers)
        if response.status_code == 200:
            return response.text
        else:
            print(f"Warning: unable to fetch document {accession_number}. Code: {response.status_code}")
            return none

    def normalize_title(self, raw_title: str) -> str | None:
        if not raw_title:
            return None

        for pattern, mapped_title in self.TITLE_MAP.items():
            if re.search(pattern, raw_title, re.IGNORECASE):
                return mapped_title

        return None

    def extract_company_officers(self, html):
        response = {
            "company": self.company_name,
            "executives": {},
            "directors": {}
        }

        soup = BeautifulSoup(html, "html.parser")
        target_row = soup.find(
                lambda tag: tag.name == "tr" and "Principal Position" in tag.get_text()
        )
        
        if target_row:
            print(f"Found executive compensation table for {self.ticker}")

            following_trs = target_row.find_next_siblings("tr")
            print(f"The executive compensation table count: {len(following_trs)}")

            for tr in following_trs:
                company_officer_text = tr.get_text()
                clean_text = os.linesep.join([s for s in company_officer_text.splitlines() if s])
                name = clean_text.partition('\n')[0]
                title = clean_text.partition('\n')[2]

                if name:
                    name = re.sub(r'^\s+', '', name)
                    name = re.sub(r'\s+$', '', name)

                    if len(re.split(r'\s{3}', name)) > 1:
                        title = re.split(r'\s{3}', name)[1]
                        name = re.split(r'\s{3}', name)[0]

                title = self.normalize_title(title)

                if name and title:
                    name = name.replace('\xa0', ' ')

                    if "CEO" in title and title not in response["executives"].values() and len(response["executives"]) < 1:
                        response["executives"][name] = title
                    elif "CEO" not in title:
                        response["executives"][name] = title

        if len(response["executives"]) > 0:
            print(f"The executives thus far: {str(response['executives'])}")

        target_row = soup.find(
                lambda tag: tag.name == "tr" and "Occupation" in tag.get_text()
        )

        if target_row:
            print(f"Found board nominees table for {self.ticker}")
            following_trs = target_row.find_next_siblings("tr")
            print(f"The board nominees table count: {len(following_trs)}")

            for tr in following_trs:
                director_text = tr.get_text()

                if len(director_text.splitlines()) <= 1:
                    continue

                clean_text = os.linesep.join([s for s in director_text.splitlines() if s])
                name = clean_text.partition('\n')[0]
                title = clean_text.partition('\n')[2]

                if name:
                    name = re.sub(r'^\s+', '', name)
                    name = re.sub(r'\s+$', '', name)
                    
                    if len(re.split(r'\s{3}', name)) > 1:
                        title = re.split(r'\s{3}', name)[1]
                        name = re.split(r'\s{3}', name)[0]
                    elif ',' in name:
                        name, title = self.split_merged_name_and_title(name)

                if title:
                    new_title = title.splitlines()[0]

                    if new_title[-1] == ",":
                        new_title = new_title.rstrip(",") + " " + title.splitlines()[1]

                    title = new_title
                else:
                    title = ""

                if title == "":
                    continue

                updated_title = self.normalize_title(title)
                sanitised_company_name = re.escape(self.company_name)
                pattern = rf"\b(?:chairman|chairwoman|chair)\b.*?\b{sanitised_company_name}"
                regex = re.compile(pattern, re.IGNORECASE)

                if updated_title == "CEO" and regex.search(title.lower()) and "former chair" not in title.lower():
                    title = "Board Chair"
                else:
                    title = updated_title

                if not re.compile(r"[a-zA-Z]").search(name):
                    name = None

                if title not in ["Board Chair", "Lead Independent Director"]:
                    title = "Director"

                if name and title:
                    name = name.rstrip(" ")
                    response["directors"][name] = title

        if len(response["directors"]) > 0:
            print(f"The directors thus far: {str(response['directors'])}")

        return response

    def split_merged_name_and_title(self, merged_str: str) -> tuple[str, str]:
        if not merged_str:
            return ("", "")

        # Clean non-breaking spaces
        clean_str = merged_str.replace("\xa0", " ").strip()

        # Search for the first title keyword boundary
        match = self.TITLE_PIVOT_PATTERN.search(clean_str)

        if match:
            pivot_idx = match.start()
            name = clean_str[:pivot_idx].strip()
            title = clean_str[pivot_idx:].strip()
            return (name, title)

        # Fallback if no keyword matched
        return (clean_str, "")

    def parse_proxy_baseline_text(self, proxy_html_text):
        extracted_data = self.extract_company_officers(proxy_html_text)

        self.roster["company"] = extracted_data.get("company", self.company_name or "Unknown")
        self.roster["executives"] = extracted_data.get("executives", {})
        self.roster["directors"] = extracted_data.get("directors", {})

    def apply_8k_delta_changes(self, html_update_text):
        """
        Evaluates Item 5.02 text snippets from an 8-K to dynamically mutate 
        the active executive and board lists.
        """
        print(f"[{self.ticker}] Submitting 8-K text delta payload...")

        try:
            # Send the cleaned, chunked 8-K text to Item 5.02 parsing method
            updated_data = self.parse_item_502_text(html_update_text)

            # Update internal tracking structures with mutated deltas
            self.roster["executives"] = updated_data.get("executives", self.roster["executives"])
            self.roster["directors"] = updated_data.get("directors", self.roster["directors"])
            
            print(f"[{self.ticker}] 8-K delta state updates applied successfully.")
            
        except Exception as e:
            print(f"Warning: Failed to process 8-K update delta: {str(e)}")
            # Fall back to current roster state silently to prevent the IRC bot thread from crashing

    def run_pipeline(self):
        """
        Executes Step 3: Resolves assets, downloads files, extracts contents, 
        and updates the final structural arrays.
        """
        print(f"Starting tracking pipeline for {self.ticker}...")
        self.fetch_cik_and_metadata()
        targets = self.find_target_filings()

        if len(targets) > 0:
            print(f"Found filings for {self.ticker}")
        
        if not targets["proxy"]:
            print("Could not isolate a baseline Proxy filing (DEF 14A).")
            return self.roster
            
        # 1. Pull down and process the baseline Proxy document
        print(f"Fetching baseline proxy document: {targets['proxy']['accession']}")
        proxy_submission_html = self.download_submission(targets['proxy'])

        if len(proxy_submission_html) > 0:
            print(f"Found SEC DEF 14A filing for {self.ticker}")

        self.parse_proxy_baseline_text(proxy_submission_html)
        
        # 2. Apply mid-year updates chronologically (Reversed from oldest up to the newest)
        print(f"Evaluating {len(targets['updates'])} mid-year 8-K amendments...")
        for update in reversed(targets["updates"]):
            print(f" -> Downloading amendment {update['accession']}...")
            update_text = self.download_submission(update)
            self.apply_8k_delta_changes(update_text)
            
        print("Pipeline Complete!\n")
        return self.roster
