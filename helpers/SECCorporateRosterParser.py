import json
import re
import requests

class SECCorporateRosterParser:
    def __init__(self, ticker, user_agent_email, GROQ_API_KEY):
        """
        Initializes the pipeline for a specific company ticker.
        The user_agent_email is mandatory to prevent the SEC from blocking requests.
        """
        self.ticker = ticker.upper()
        self.GROQ_API_KEY = GROQ_API_KEY

        from openai import OpenAI

        self.client = OpenAI(
            api_key=self.GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1"
        )

        self.headers = {
            "User-Agent": f"IRCInvestmentBot/2.0 ({user_agent_email})"
        }
        self.cik = None
        self.company_name = None
        self.recent_filings = None
        
        # The ultimate structured tracking roster for your IRC Bot output
        self.roster = {
            "company": "Unknown",
            "executives": {},    # Schema: {"Executive Name": "Corporate Title"}
            "board_members": {}  # Schema: {"Director Name": "Board Title"}
        }

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

    def download_raw_submission_txt(self, accession_number):
        """
        Constructs and downloads the full text container file (.txt) via the SEC Archive Server
        """
        unpadded_cik = str(int(self.cik)) # Folders match stripping out leading zeros
        stripped_accession = accession_number.replace("-", "")
        
        # Standard SEC format for complete container text streams
        txt_url = f"https://www.sec.gov/Archives/edgar/data/{unpadded_cik}/{stripped_accession}/{accession_number}.txt"
        
        response = requests.get(txt_url, headers=self.headers)
        if response.status_code == 200:
            return response.text
        else:
            print(f"Warning: Unable to fetch document {accession_number}. Code: {response.status_code}")
            return None

    def extract_document_body(self, raw_txt_content):
        """
        Isolates the document text body nested within the SGML structural file wrapper tags.
        """

        if not raw_txt_content:
            return ""

        # Pulls out content between the foundational <TEXT> envelopes
        body_match = re.search(r"<TEXT>(.*?)</TEXT>", raw_txt_content, re.DOTALL | re.IGNORECASE)

        text_block = body_match.group(1) if body_match else raw_txt_content
        text_block = re.sub(r"<style[^>]*>.*?</style>", "", text_block, flags=re.DOTALL | re.IGNORECASE)
        text_block = re.sub(r"<script[^>]*>.*?</script>", "", text_block, flags=re.DOTALL | re.IGNORECASE)
        text_block = re.sub(r"<[^>]+>", " ", text_block)
        text_block = re.sub(r'\s+', ' ', text_block).strip()

        return text_block[:32000]

    def process_with_groq(self, text_content, instruction_prompt):
        """
        A centralized inference engine to process layout strings via the Groq API setup.
        """
        try:
            response = self.client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a precise data extraction assistant. Your job is to extract corporate governance "
                            "rosters from SEC text and output ONLY valid JSON matching the requested schema. Do not include markdown formatting like ```json or any conversational text."
                        )
                    },
                    {
                        "role": "user",
                        "content": f"{instruction_prompt}\n\nSEC TEXT SUBMISSION:\n{text_content}"
                    }
                ],
                temperature=0.0
            )

            raw_output = response.choices[0].message.content.strip()
            raw_output = re.sub(r"^```json\s*|\s*```$", "", raw_output, flags=re.IGNORECASE)
            return json.loads(raw_output)
        except json.JSONDecodeError:
            raise Exception("Groq failed to return a perfectly formatted JSON string. Try again.")
        except Exception as e:
            raise Exception(f"Groq API Error: {str(e)}")

    def parse_proxy_baseline_text(self, proxy_html_text):
        """
        Processes the clean text payload from the DEF 14A via Groq.
        """
        prompt = (
                f"Extract the official company name, the list of current executive officers (C-suite), "
                f"and all members of the Board of Directors for ticker {self.ticker}. "
                f"Output your final response as a JSON object matching this structural schema precisely:\n"
                f"{{\n"
                f"  \"company\": \"Full Company Name\",\n"
                f"  \"executives\": {{\n"
                f"    \"Name\": \"Title\"\n"
                f"  }},\n"
                f"  \"board_members\": {{\n"
                f"    \"Name\": \"Title\"\n"
                f"  }}\n"
                f"}}\n"
                f"For the executives, only return the CEO, CFO, CTO and COO. Try and remap alternative titles to those four titles. So for example, President would be COO, Chief Executive would be CEO, Managing Director or MD would be CEO, etc.\n"
                f"Unlike the executives, however, all members of the board should be returned, comprising both their name and their title, which should default to Director; otherwise their explicitly named title, such as Exec Chair / Executive Chair / Board Chair / Independent Board Director, should be used instead."
        )

        extracted_data = self.process_with_groq(proxy_html_text, prompt)

        self.roster["company"] = extracted_data.get("company", self.company_name or "Unknown")
        self.roster["executives"] = extracted_data.get("executives", {})
        self.roster["board_members"] = extracted_data.get("board_members", {})

    def apply_8k_delta_changes(self, update_html_text):
        """
        Evaluates Item 5.02 text snippets from an 8-K to dynamically mutate 
        the active executive and board lists.
        """
        print(f"[{self.ticker}] Submitting 8-K text delta payload to Groq...")

        # Construct a precise prompt passing the current live dictionary state
        prompt = (
            f"You are analyzing an SEC Form 8-K (Item 5.02) filing for {self.ticker}.\n"
            f"Your objective is to read the document text and apply any specified changes to the company's "
            f"current executive roster and board of directors.\n\n"
            f"CRITICAL RULES:\n"
            f"1. If an individual resigns, departs, retires, or steps down, REMOVE them from the list.\n"
            f"2. If an individual is newly appointed or elected, ADD them with their proper title.\n"
            f"3. Do not modify or remove any names unless explicitly triggered by the filing text.\n\n"
            f"--- CURRENT ROSTER STATE ---\n"
            f"{json.dumps(self.roster, indent=2)}\n\n"
            f"Return the exact updated roster matching the structural format of the initial state. "
            f"Output ONLY the raw JSON string matching the keys 'company', 'executives', and 'board_members'."
        )
        
        try:
            # Send the cleaned, chunked 8-K text directly to Groq using the generic method
            updated_data = self.process_with_groq(update_html_text, prompt)
            
            # Update internal tracking structures with mutated deltas
            self.roster["executives"] = updated_data.get("executives", self.roster["executives"])
            self.roster["board_members"] = updated_data.get("board_members", self.roster["board_members"])
            
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
        
        if not targets["proxy"]:
            print("Could not isolate a baseline Proxy filing (DEF 14A).")
            return self.roster
            
        # 1. Pull down and process the baseline Proxy document
        print(f"Fetching baseline proxy document: {targets['proxy']['accession']}")
        raw_proxy_container = self.download_raw_submission_txt(targets['proxy']['accession'])
        clean_proxy_text = self.extract_document_body(raw_proxy_container)
        self.parse_proxy_baseline_text(clean_proxy_text)
        
        # 2. Apply mid-year updates chronologically (Reversed from oldest up to the newest)
        print(f"Evaluating {len(targets['updates'])} mid-year 8-K amendments...")
        for update in reversed(targets["updates"]):
            print(f" -> Downloading amendment {update['accession']}...")
            raw_update_container = self.download_raw_submission_txt(update["accession"])
            clean_update_text = self.extract_document_body(raw_update_container)
            self.apply_8k_delta_changes(clean_update_text)
            
        print("Pipeline Complete!\n")
        return self.roster
