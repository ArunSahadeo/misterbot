import irc.client
import irc.connection
import ssl
from datetime import datetime
import logging
import socket
import re
import requests
from bs4 import BeautifulSoup
from user_agent import generate_user_agent
import tempfile
import os
import time
from urllib.parse import urlparse, quote
import yfinance as yf
from multiprocessing import Process, Queue
from playwright.sync_api import sync_playwright, TimeoutError
from playwright._impl._errors import Error as PlaywrightError
from playwright_stealth.stealth import Stealth
import math
import sys
import json
import random
import traceback
from lxml import etree, html

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ollama_server_url = 'http://localhost:11434/api/generate'

class IRCBot(irc.client.SimpleIRCClient):
    def __init__(self, config):
        """Initialize the bot with configuration, including SASL and SSL."""
        super().__init__()
        self.server = config['server']
        self.port = config['port']
        self.nickname = config['nickname']
        self.sasl_username = config['sasl_username']
        self.sasl_password = config['sasl_password']
        self.channels = config['channels']
        self._channel = None
        self._target_user = None
        self.nickserv_requests = {}
        self.command_handlers = {
            '!time': self.handle_time,
            '!news': self.handle_news,
            '!convert': self.handle_conversion,
            '!seen': self.handle_last_seen,
            '!quote': self.handle_stock_quote,
            '.q': self.handle_stock_quote,
            '.t': self.handle_stock_info,
            '.market': self.handle_market_prices,
            '.markets': self.handle_market_prices,
            '.bond': self.handle_bond_prices,
            '.bonds': self.handle_bond_prices,
            '.yield': self.handle_bond_prices,
            '.yields': self.handle_bond_prices,
            '.oil': self.handle_oil_prices,
            '.currency': self.handle_currency_prices,
            '.crypto': self.handle_crypto_prices,
            '.c': self.handle_crypto_prices
        }
        # Flag to track SASL success

    def format_number(self, n):
        if n is None:
            return "N/A"
        suffixes = ['', 'K', 'M', 'B', 'T']
        magnitude = 0
        while abs(n) >= 1000:
            magnitude += 1
            n /= 1000.0
        return '{:.1f}{}'.format(n, suffixes[magnitude])

    def extract_metadata(self, docx_file):
        import docx

        doc = docx.Document(docx_file)  # Create a Document object from the Word document file.
        core_properties = doc.core_properties  # Get the core properties of the document.
        metadata = {}  # Initialize an empty dictionary to store metadata
        # Extract core properties
        for prop in dir(core_properties):  # Iterate over all properties of the core_properties object.
            if prop.startswith('__'):  # Skip properties starting with double underscores (e.g., __elenent). Not needed
                continue
            value = getattr(core_properties, prop)  # Get the value of the property.
            if callable(value):  # Skip callable properties (methods).
                continue
            if prop == 'created' or prop == 'modified' or prop == 'last_printed':  # Check for datetime properties.
                if value:
                    value = value.strftime('%Y-%m-%d %H:%M:%S')  # Convert datetime to string format.
                else:
                    value = None
            metadata[prop] = value  # Store the property and its value in the metadata dictionary.
        # Extract custom properties (if available).
        try:
            custom_properties = core_properties.custom_properties  # Get the custom properties (if available).
            if custom_properties:  # Check if custom properties exist.
                metadata['custom_properties'] = {}  # Initialize a dictionary to store custom properties.
                for prop in custom_properties:  # Iterate over custom properties.
                    metadata['custom_properties'][prop.name] = prop.value  # Store the custom property name and value.
        except AttributeError:
            # Custom properties not available in this version.
            pass  # Skip custom properties extraction if the attribute is not available.
        return metadata  # Return the metadata dictionary.

    def get_financial_news(self, keyword: str):
        logger.debug(f"The ticker in get_financial_news: {keyword}")

        try:
            headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
            }
            url = f"https://finviz.com/quote.ashx?t={keyword}"
            response = requests.get(url, headers=headers)
            news_items = []

            if response.status_code == 200:
                html = response.text
                soup = BeautifulSoup(html, "html.parser")
                for index, news_item in zip(range(5), soup.select('#news-table > tr')):
                    news_item_dict = {
                        'link': news_item.find('a').get('href'),
                        'title': news_item.find('a').text
                    }

                    news_items.append(news_item_dict)
                    
                return news_items
            else:
                return [f"Error: {response.status_code} from Finviz"]
        except Exception as e:
            return [f"Exception fetching news: {str(e)}"]

    def summarise_article(self, news_item):
        default_response = f"{news_item['title']} ({news_item['link']})"
        logger.debug(f"The news_item: {default_response}")

        if 'youtube.com' in news_item['link'] or 'barrons.com' in news_item['link']:
            return default_response

        link = news_item['link']

        if link.startswith('/'):
            link = 'https://finviz.com' + link

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
            }
            response = requests.get(link, headers=headers)
            if response.status_code == 200:
                html = response.text
                soup = BeautifulSoup(html, "html.parser")

                if "finance.yahoo.com" in link:
                    text = soup.find("div", {"class": "atoms-wrapper"}).text
                elif "finviz.com/news" in link:
                    text = soup.find("div", {"class": "news-content"}).text
                else:
                    text = default_response
            else:
                return [f"Error: {response.status_code} from {link}"]
        except Exception as e:
            return [f"Exception summarising article: {str(e)}"]

        if text == default_response or len(text) >= 4000:
            return text

        prompt = f"Summarise the following article in 256 characters or less:\n\n{text}"
         
        data = {
            "model": "llama3.2",
            "prompt": prompt,
            "stream": False,
            "max_length": 80
        }

        try:
            response = requests.post(ollama_server_url, json=data)
            if response.status_code == 200:
                result = response.json()
                return result.get("response", "").strip()
            else:
                return f"Error: Server responded with {response.status_code}."
        except requests.exceptions.RequestException as e:
            return f"Error: Unable to reach Ollama server. {e}"

    def on_all_raw_messages(self, connection, event):
        raw_message = event.arguments[0]

        if raw_message.startswith('PING '):
            ping_arg = re.sub(r"^PING ", "", raw_message)
            #pong_response = f":{ping_arg}" if not ping_arg.startswith(":") else ping_arg
            pong_response = ping_arg
            connection.pong(pong_response)
            logger.debug(f"Received PING {ping_arg}, sent PONG {pong_response}, as a raw message.")

    def on_connect(self, connection, event):
        """Called when bot connects to the server."""
        logger.debug("Connected to server")

    def on_welcome(self, connection, event):
        """Called when bot is fully connected and welcomed."""
        logger.info("Received welcome message")
        self.sasl_authenticated = True
        for channel in self.channels:
            connection.join(channel)
            logger.info(f"Joined channel {channel}")

    def on_ping(self, connection, event):
        ping_arg = event.arguments[0]
        #pong_response = f":{ping_arg}" if not ping_arg.startswith(":") else ping_arg
        pong_response = ping_arg
        connection.pong(pong_response)
        logger.debug(f"Received PING {ping_arg}, sent PONG {pong_response}")

    def on_disconnect(self, connection, event):
        """Called when bot disconnects."""
        logger.error("Disconnected from server")
        self.sasl_authenticated = False
        main()

    def on_sasl_authenticated(self, connection, event):
        """Called when SASL authentication succeeds."""
        logger.info("SASL authentication successful")
        self.sasl_authenticated = True

    def on_sasl_failed(self, connection, event):
        """Called when SASL authentication fails."""
        logger.error("SASL authentication failed, attempting manual NickServ IDENTIFY")
        connection.privmsg("NickServ", f"IDENTIFY {self.sasl_username} {self.sasl_password}")

    def on_privnotice(self, connection, event):
        source = event.source.nick.lower()
        message = event.arguments[0]

        if source == "nickserv":
            match = re.search(r"User seen\s+:\s+(.+)", message)

            if match:
                last_seen_info = match.group(1)
                last_seen_info = last_seen_info.replace("(", "").replace(")", "")

                for user, (requester, _) in self.nickserv_requests.items():
                    if user == self._target_user:
                        connection.privmsg(self._channel,
                            f"{requester}: {user} was last seen {last_seen_info}")

                        break

    def on_pubmsg(self, connection, event):
        """Handle public channel messages."""
        message = event.arguments[0]
        pattern = r"(?P<url>https?://[^\s]+)"
        urls = re.findall(pattern, message)
        sender = event.source.nick
        channel = event.target

        if message.startswith('!'):
            command = message.split()[0]
            if command in self.command_handlers:
                try:
                    self.command_handlers[command](connection, sender, message, channel)
                except Exception as e:
                    logger.error(f"Error handling command {command} in {channel} from on_pubmsg: {e}")
                    connection.privmsg(channel, f"Error processing command: {e}")
        elif re.match('^\.[a-z]{1,}', message):
            command = message.split()[0]
            if command in self.command_handlers:
                try:
                    self.command_handlers[command](connection, sender, message, channel)
                except Exception as e:
                    logger.error(f"Error handling command {command} in {channel} from on_pubmsg: {e}")
                    connection.privmsg(channel, f"Error processing command: {e}")
        elif len(urls) > 0:
            for url in urls:
                self.output_link(url, connection, channel)

    def human_like_interaction(self, page):
        page.mouse.move(100, 100)
        time.sleep(random.uniform(0.5, 1.5))
        page.mouse.move(200, 300)
        time.sleep(random.uniform(0.5, 1.5))
        page.evaluate("window.scrollBy(0, window.innerHeight / 2)")
        time.sleep(random.uniform(1, 2))

    def run_playwright(self, url, queue):
        """Run Playwright in a separate process to fetch page metadata."""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/140.0.0.0 Safari/537.36"
                    ),
                    locale="en-GB",
                    viewport={"width": 1280, "height": math.floor(random.random() * 100)},
                    java_script_enabled=True,
                    color_scheme="light",
                    timezone_id="Europe/Paris",
                )
                page = context.new_page()
                stealth_manager = Stealth()
                stealth_manager.apply_stealth_sync(page)
                tempdir = tempfile.gettempdir()
                is_document = False

                try:
                    response = page.goto(url, wait_until="networkidle", timeout=10000)
                except PlaywrightError:
                    response = page.request.get(url, timeout=10000)
                    is_document = True

                if not is_document:
                    self.human_like_interaction(page)
                    page.reload()

                content_type = response.headers.get("content-type", "")
                basename = os.path.basename(url)
                message = ""

                if "application/pdf" in content_type:
                    content_bytes = response.body()
                    with open(os.path.join(tempdir, basename), "wb") as f:
                        f.write(content_bytes)
                        title = os.popen(f"pdfinfo {f.name} | grep 'Title:'").read()
                        title = re.sub(r"Title:\s+", "", title)
                        f.close()
                        os.unlink(f.name)
                        if title == '':
                            title = basename
                        title = title.replace("\r", "").replace("\n", "")
                        message = f"[ {title} ]"
                elif content_type in ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"]:
                    content_bytes = response.body()
                    with open(os.path.join(tempdir, os.path.basename(url)), "wb") as f:
                        f.write(content_bytes)
                        metadata = self.extract_metadata(f.name)
                        title = metadata.get('title', '')
                        author = metadata.get('author', '')
                        created = metadata.get('created', '')
                        last_modified = metadata.get('last_modified', '')
                        last_modified_by = metadata.get('last_modified_by', '')
                        f.close()
                        os.unlink(f.name)
                        if title == '':
                            title = basename
                        title = title.replace("\r", "").replace("\n", "")
                        message = f"[ Title: {title} ] [ Author: {author} ] [ Created: {created} ] [ Last Modified: {last_modified} ] [ Last Modified By: {last_modified_by} ]"

                page_title = page.title()
                default_title = ""

                if "x.com" in url and "fixupx.com" not in url:
                    logger.debug(f"The page title: {page_title}")
                    try:
                        page.wait_for_selector("[data-testid=\"UserName\"]", timeout=15000)
                        logger.debug("✅ Found tweeter username")
                        username_wrapper = page.query_selector('[data-testid="UserName"] > div > div > div > div')
                        title = username_wrapper.inner_text() if username_wrapper else page_title
                        message = f"[ {title} ]"
                    except TimeoutError:
                        logger.debug("Timeout waiting for tweeter username.")

                if "fixupx.com" in url:
                    try:
                        page.wait_for_selector("article", timeout=15000)
                        logger.debug("✅ Found main tweet")
                        meta = page.query_selector('meta[property="og:title"]')
                        title = meta.get_attribute("content") if meta else None
                        meta2 = page.query_selector('meta[property="og:description"]')
                        description = meta2.get_attribute("content") if meta2 else None
                        if title:
                            logger.debug("✅ Found tweet author")
                            title = title.replace("\r", "").replace("\n", "")
                            title = re.sub(r'\u200b', '', title)
                            if description:
                                logger.debug("✅ Found tweet content")
                                description = description.replace("\r", "").replace("\n", "")
                                message = f"[ {title}: {description} ]"
                            else:
                                description = "Check the tweet for any attached media."
                                message = f"[ {title}: {description} ]"
                    except TimeoutError:
                        logger.debug("Timeout waiting for tweet content.")

                if "bsky." in url:
                    try:
                        page.wait_for_selector("div[data-testid*=\"postThreadItem-by-\"]", timeout=15000)
                        logger.debug("✅ Found main Bluesky post")
                        meta = page.query_selector('meta[property="og:title"]')
                        title = meta.get_attribute("content") if meta else None
                        meta2 = page.query_selector('meta[property="og:description"]')
                        description = meta2.get_attribute("content") if meta2 else None
                        if title:
                            logger.debug("✅ Found Bluesky post author")
                            title = title.replace("\r", "").replace("\n", "")
                            if description:
                                logger.debug("✅ Found Bluesky post content")
                                description = description.replace("\r", "").replace("\n", "")
                                message = f"[ {title}: {description} ]"
                                logger.debug(f"The byte count of the string: {len(message.encode('utf-8'))}")

                                if len(message.encode('utf-8')) >= 495:
                                    message = message[:447] + '...'
                                    logger.debug(f"The byte count of the string: {len(message.encode('utf-8'))}")
                            else:
                                description = "Check the Bluesky post for any attached media."
                                message = f"[ {title}: {description} ]"
                    except TimeoutError:
                        logger.debug("Timeout waiting for Bluesky post content.")

                if re.match(r'^(?:https:\/\/)?archive\.', url):
                    default_title = re.sub(r'^https:\/\/', '', url)
                    default_title = re.sub(r'\/$', '', default_title)
                    try:
                        page.wait_for_function(
                            f"document.title !== '{default_title}'",
                            timeout=15000
                        )
                        page_title = page.title()
                    except TimeoutError:
                        logger.debug("Timeout waiting for archive.* title to change.")

                if "ft.com" in url and "Subscribe to read" in page_title:
                    try:
                        page.wait_for_selector("blockquote", timeout=15000)
                        logger.debug("✅ Found main blockquote")
                        blockquote = page.query_selector('blockquote')
                        title = blockquote.inner_text() if blockquote else "Subscribe to read"
                        message = f"[ {title} ]"
                    except TimeoutError:
                        logger.debug("Timeout waiting for FT blockquote.")

                if "youtube.com" in url:
                    try:
                        page.wait_for_selector("ytd-channel-name a", timeout=15000)
                        logger.debug("✅ Found main channel name")
                        channel_element = page.query_selector('ytd-channel-name a')
                        title = page_title

                        if channel_element:
                            logger.debug("✅ Found main channel name element")
                            channel_name = channel_element.inner_text().strip()
                            new_title = re.sub(r"- YouTube$", "- " + channel_name, title)

                            if title != new_title:
                                title = new_title

                            if title.startswith('-'):
                                title_element = page.query_selector('#title > h1 > yt-formatted-string')
                                title_element_text = title_element.inner_text().strip()
                                title = title_element_text + ' ' + title

                        message = f"[ {title} ]"
                    except TimeoutError:
                        logger.debug("Timeout waiting for YouTube channel name.")

                if "instagram.com" in url:
                    try:
                        page.wait_for_selector("img", timeout=15000)
                        logger.debug("✅ Found image")
                        img = page.query_selector('img')
                        description = img.get_attribute("alt") if img else None
                        author = page.query_selector('//div[text()="Follow"]/../preceding-sibling::*[1]')
                        title = "Instagram"
                        if author:
                            author = author.inner_text()
                        message = f"[ {author}: {description} ]"
                    except TimeoutError:
                        logger.debug("Timeout waiting for Instagram content.")

                if len(message) < 1:
                    message = f"[ {page_title} ]"

                try:
                    page.screenshot(path="screenshot.png", full_page=True, timeout=10000)
                except:
                    pass

                html = page.content()

                with open('./html.txt', 'w') as html_file:
                    print(html, file=html_file)
                    html_file.close()

                browser.close()
                queue.put(message)
        except Exception as e:
            logger.error(f"Playwright process failed for {url}: {e}")
            queue.put(f"Error processing {url}: {str(e)}")

    def output_link(self, url, connection, channel):
        """Handle URL processing in a separate process using Playwright."""
        domain = urlparse(url)
        domain = domain.netloc.replace('www.', '')

        if ("twitter.com" in url or re.search("http(?:[s])?://x.com", url) or re.search("^x.com", url) or "xcancel.com" in url) and ("/status/" in url):
            url = re.sub(r"(?:x|twitter|xcancel)\.com", "fixupx.com", url)
            url = re.sub(r"vxfixupx\.com", "fixupx.com", url)

        message = ""

        if domain in ["reuters.com"]:
            try:
                response = requests.get(url)
                if response.status_code == 200:
                    html = response.text
                    soup = BeautifulSoup(html, "html.parser")
                    title = soup.find("title").text
                    message = f"[ {title} ]"
            except Exception as e:
                logger.debug(f"Exception fetching Reuters link: {str(e)}")
                return

        if len(message) > 0:
            connection.privmsg(channel, f"{message}")
            return

        queue = Queue()
        process = Process(target=self.run_playwright, args=(url, queue))
        process.start()
        process.join(60)  # Wait up to 15 seconds for the process to complete
        if process.is_alive():
            process.terminate()
            logger.debug(f"Timeout processing URL {url}")
            connection.privmsg(channel, f"Timeout processing {url}")
            return
        message = queue.get()
        message = message.replace("\n", "")

        try:
            connection.privmsg(channel, f"{message}")
        except irc.client.MessageTooLong as e:
            logger.debug(f"Message too long: {str(e)}")
            connection.privmsg(channel, f"Message too long: {str(e)}")

    def handle_time(self, connection, sender, message, channel):
        """Handle !time command."""
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        connection.privmsg(channel, f"Current time: {current_time}")

    def handle_news(self, connection, sender, message, channel):
        """Handle !news command."""
        ticker = re.sub(r"^!news ", "", message)
        ticker = re.sub(r"^\$", "", ticker)
        logger.debug(f"The ticker: {ticker}")
        news_items = self.get_financial_news(ticker)
        
        for news_item in news_items:
            summary = self.summarise_article(news_item)
            logger.debug(f"{summary}")
            connection.privmsg(channel, f"{summary}")

    def handle_last_seen(self, connection, sender, message, channel):
        """Handle !seen command."""
        target = re.sub(r"^!seen ", "", message)
        target = re.sub(r"^ ", "", target)
        self.nickserv_requests[target.lower()] = (sender, time.time())
        self._channel = channel
        self._target_user = target.lower()
        connection.privmsg("NickServ", f"INFO {target}")

    def handle_bond_prices(self, connection, sender, message, channel):
        """Handle .bond / .bonds / .yield / .yields command."""

        """
        {
            'index': '^IRX',
            'name': '13W'
        },
        """

        bond_indices = [
            {
                'index': 'US1Y',
                'name': '1Y'
            },
            {
                #'index': '2YY=F',
                'index': 'US2Y',
                'name': '2Y',
            },
            {
                #'index': '^FVX',
                'index': 'US5Y',
                'name': '5Y'
            },
            {
                #'index': '^TNX',
                'index': 'US10Y',
                'name': '10Y',
            },
            {
                #'index': '^TYX',
                'index': 'US30Y',
                'name': '30Y'
            }
        ]

        message = ""

        for bond in bond_indices:
            #url = f"https://finance.yahoo.com/quote/{quote(bond['index'])}"
            url = f"https://www.cnbc.com/quotes/{quote(bond['index'])}"

            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
                }

                response = requests.get(url, headers=headers)

                if response.status_code == 200:
                    html = response.text
                    soup = BeautifulSoup(html, "html.parser")
                    #price = soup.select('[data-testid="qsp-price"]')
                    price = soup.select('h3.Summary-title + ul > li.Summary-stat:nth-child(5) > span.Summary-value')
                    bond_yield = soup.select('.QuoteStrip-lastPrice')
                    #relative_change = soup.select('[data-testid="qsp-price-change-percent"]')
                    price_at_previous_close = soup.select('h3.Summary-title + ul > li.Summary-stat:nth-child(8) > span.Summary-value')
                    #//h3[contains(@class, 'Summary-title') and contains(text(), 'KEY STATS')]

                    #if len(price) < 1 or len(relative_change) < 1:
                    if len(price) < 1 or len(price_at_previous_close) < 1 or len(bond_yield) < 1:
                        continue

                    price = str(price[0].text).strip()
                    bond_yield = str(bond_yield[0].text).strip()
                    price_at_previous_close = str(price_at_previous_close[0].text).strip()

                    '''
                    relative_change = str(relative_change[0].text).strip()

                    relative_change = re.sub(r'[()]', '', relative_change)
                    '''

                    try:
                        current_price = float(price)
                        previous_price = float(price_at_previous_close)

                        difference = ((current_price - previous_price) / previous_price) * 100
                        difference = math.floor(difference * 10 ** 4) / 10 ** 4
                        relative_change = (str(difference) + '%')

                        if relative_change.startswith('.'):
                            relative_change = '+0' + relative_change
                    except:
                        pass

                    relative_change_format_start = ""
                    relative_change_format_end = ""

                    if '-' in relative_change:
                        relative_change_format_start = "\x034"
                        relative_change_format_end = "\x0F"
                    else:
                        relative_change_format_start = "\x033"
                        relative_change_format_end = "\x0F"
                    
                    if len(message) < 1:
                        message += f"{bond['name']}: {price} (Price) {bond_yield} (Yield) {relative_change_format_start}{relative_change}{relative_change_format_end} (Price Change)"
                    else:
                        message += f" {bond['name']}: {price} (Price) {bond_yield} (Yield) {relative_change_format_start}{relative_change}{relative_change_format_end} (Price Change)"
                else:
                    logger.debug(f"Couldn't fetch bond data for {bond['name']}: {str(e)}")
            except Exception as e:
                logger.debug(f"Exception querying bond maturity {bond['name']} from {url}")
            
        connection.privmsg(channel, message)

    def handle_oil_prices(self, connection, sender, message, channel):
        """Handle .oil command."""

        oil_indices = [
            {
                'index': '@CL.1',
                'name': 'WTI Crude'
            },
            {
                'index': '@LCO.1',
                'name': 'ICE Brent Crude',
            },
            {
                'index': '@NG.1',
                'name': 'Nat Gas'
            },
            {
                'index': '@RB.1',
                'name': 'RBOB Gas',
            },
        ]

        message = ""

        for oil_index in oil_indices:
            url = f"https://www.cnbc.com/quotes/{quote(oil_index['index'])}"

            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
                }

                response = requests.get(url, headers=headers)

                if response.status_code == 200:
                    html = response.text
                    soup = BeautifulSoup(html, "html.parser")
                    price = soup.select('.QuoteStrip-lastPrice')
                    relative_change = soup.select('.QuoteStrip-lastPriceStripContainer > *:last-child > *:last-child')

                    if len(price) < 1 or len(relative_change) < 1:
                        continue

                    price = str(price[0].text).strip()
                    relative_change = str(relative_change[0].text).strip().replace('(', '').replace(')', '')

                    relative_change_format_start = ""
                    relative_change_format_end = ""

                    if '-' in relative_change:
                        relative_change_format_start = "\x034"
                        relative_change_format_end = "\x0F"
                    else:
                        relative_change_format_start = "\x033"
                        relative_change_format_end = "\x0F"
                    
                    if len(message) < 1:
                        message += f"{oil_index['name']}: {price} {relative_change_format_start}{relative_change}{relative_change_format_end}"
                    else:
                        message += f" {oil_index['name']}: {price} {relative_change_format_start}{relative_change}{relative_change_format_end}"
                else:
                    logger.debug(f"Couldn't fetch oil data for {oil_index['name']}: {str(e)}")
            except Exception as e:
                logger.debug(f"Exception querying oil index {oil_index['name']} from {url}")
            
        connection.privmsg(channel, message)

    def handle_currency_prices(self, connection, sender, message, channel):
        """Handle .currency command."""

        currencies = [
            {
                'index': 'GBP=',
                'name': 'GBPUSD'
            },
            {
                'index': 'JPY=X',
                'name': 'USDJPY',
            },
            {
                'index': 'EUR=X',
                'name': 'EURUSD'
            },
            {
                'index': 'CNY=',
                'name': 'USDCNY',
            },
            {
                'index': 'CAD=',
                'name': 'USDCAD',
            },
            {
                'index': 'MXN=',
                'name': 'USDMXN',
            },
            {
                'index': '@GC.1',
                'name': 'GOLD',
            },
            {
                'index': 'BTC.CB=',
                'name': 'BTC-USD',
            },
        ]

        message = ""

        for currency in currencies:
            url = f"https://www.cnbc.com/quotes/{quote(currency['index'])}"

            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
                }

                response = requests.get(url, headers=headers)

                if response.status_code == 200:
                    html = response.text
                    soup = BeautifulSoup(html, "html.parser")
                    price = soup.select('.QuoteStrip-lastPrice')
                    relative_change = soup.select('.QuoteStrip-lastPriceStripContainer > *:last-child > *:last-child')

                    if len(price) < 1 or len(relative_change) < 1:
                        continue

                    price = str(price[0].text).strip()
                    relative_change = str(relative_change[0].text).strip().replace('(', '').replace(')', '')

                    relative_change_format_start = ""
                    relative_change_format_end = ""

                    if '-' in relative_change:
                        relative_change_format_start = "\x034"
                        relative_change_format_end = "\x0F"
                    else:
                        relative_change_format_start = "\x033"
                        relative_change_format_end = "\x0F"
                    
                    if len(message) < 1:
                        message += f"{currency['name']}: {price} {relative_change_format_start}{relative_change}{relative_change_format_end}"
                    else:
                        message += f" {currency['name']}: {price} {relative_change_format_start}{relative_change}{relative_change_format_end}"
                else:
                    logger.debug(f"Couldn't fetch currency pair for {currency['name']}: {str(e)}")
            except Exception as e:
                logger.debug(f"Exception querying currency pair {currency['name']} from {url}")
            
        connection.privmsg(channel, message)

    def handle_crypto_prices(self, connection, sender, message, channel):
        """Handle .crypto command."""

        ticker = re.sub(r"^\.(crypto|c) ", "", message)

        if re.match("^\$", ticker):
            ticker = re.sub(r"^\$", "", ticker)

        message = ""
        url = "https://coinmarketcap.com/"

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
            }

            response = requests.get(url, headers=headers)

            if response.status_code == 200:
                tree = html.fromstring(response.text)
                price = tree.xpath(f"//p[contains(@class, 'coin-item-symbol') and text() = '{ticker.upper()}']/ancestor::td/following-sibling::td[1]")[0]
                name = tree.xpath(f"//p[contains(@class, 'coin-item-symbol') and text() = '{ticker.upper()}']/parent::div/preceding-sibling::p/text()")[0]
                one_hour_percent_change = tree.xpath(f"//p[contains(@class, 'coin-item-symbol') and text() = '{ticker.upper()}']/ancestor::td/following-sibling::td[2]")[0]
                one_hour_percent_change_element = etree.tostring(one_hour_percent_change)
                one_hour_percent_change_element = one_hour_percent_change_element.decode('ascii')
                price_element = etree.tostring(price)
                price_element = price_element.decode('ascii')
                price_element = re.sub('<[^<]+?>', '', price_element)
                price = price_element

                relative_change_format_start = ""
                relative_change_format_end = ""

                if 'icon-Caret-down' in one_hour_percent_change_element:
                    relative_change_format_start = "\x034"
                    relative_change_format_end = "\x0F"
                else:
                    relative_change_format_start = "\x033"
                    relative_change_format_end = "\x0F"

                one_hour_percent_change_element = re.sub('<[^<]+?>', '', one_hour_percent_change_element)
                one_hour_percent_change = one_hour_percent_change_element

                message = f"{ticker.upper()}: {name} | {price} | {relative_change_format_start}{one_hour_percent_change}{relative_change_format_end} (1hr)"
        except Exception as e:
            return [f"Couldn't fetch coin data for {ticker}: {str(e)}"]

        connection.privmsg(channel, message)

    def handle_market_prices(self, connection, sender, message, channel):
        """Handle .market / .markets command."""

        market_indices = [
            {
                #'index': '^DJI',
                'index': '.DJI',
                'name': 'Dow Jones'
            },
            {
                #'index': '^GSPC',
                'index': '.SPX',
                'name': 'S&P 500',
            },
            {
                #'index': '^RUT',
                'index': '.RUT',
                'name': 'Russell 2000',
            },
            {
                #'index': '^IXIC',
                'index': '.IXIC',
                'name': 'NASDAQ Composite'
            },
            {
                #'index': '^VIX',
                'index': '.VIX',
                'name': 'VIX'
            }
        ]

        message = ""

        for market_index in market_indices:
            #url = f"https://finance.yahoo.com/quote/{market_index['index']}/"
            url = f"https://www.cnbc.com/quotes/{market_index['index']}"

            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
                }

                response = requests.get(url, headers=headers)

                if response.status_code == 200:
                    html = response.text
                    soup = BeautifulSoup(html, "html.parser")
                    #price = soup.select('[data-testid="qsp-price"]')
                    price = soup.select('.QuoteStrip-lastPrice')
                    #relative_change = soup.select('[data-testid="qsp-price-change-percent"]')
                    relative_change = soup.select('.QuoteStrip-lastPriceStripContainer > span:last-child > span:last-child')

                    if len(price) < 1 or len(relative_change) < 1:
                        continue

                    price = str(price[0].text).strip()
                    relative_change = str(relative_change[0].text).strip()
                    relative_change = re.sub(r'[()]', '', relative_change)
                    relative_change_format_start = ''
                    relative_change_format_end = ''

                    if '+' in relative_change:
                        relative_change_format_start = "\x033"
                        relative_change_format_end = "\x0F"
                    else:
                        relative_change_format_start = "\x034"
                        relative_change_format_end = "\x0F"
                    
                    if len(message) < 1:
                        message += f"{market_index['name']}: {price} {relative_change_format_start}{relative_change}{relative_change_format_end}"
                    else:
                        message += f" {market_index['name']}: {price} {relative_change_format_start}{relative_change}{relative_change_format_end}"
            except Exception as e:
                logger.debug(f"Exception querying market index {market_index['name']} from {url}")
            
        connection.privmsg(channel, message)

    def handle_stock_quote(self, connection, sender, message, channel):
        """Handle !quote / .q command."""

        import yfinance as yf

        ticker = re.sub(r"^!quote ", "", message)
        ticker = re.sub(r"^\.q ", "", message)
        ticker = re.sub(r"^ ", "", ticker)

        if re.match("^\$", ticker):
            ticker = re.sub(r"^\$", "", ticker)

        stock = yf.Ticker(ticker)
        data = stock.info
        price = data.get("currentPrice")

        if price is None:
            price = data.get("regularMarketPrice")

        previous_price = data.get("regularMarketPreviousClose", 0.0)
        relative_change = ((price / previous_price) - 1.0) * 100.0
        absolute_change = price - previous_price
        volume = data.get("volume", None)
        market_cap = data.get("marketCap", None)
        stock_name = data.get("longName", "N/A")

        if stock_name == "N/A":
            stock_name = data.get("displayName", "N/A")

        fifty_two_week_range = data.get("fiftyTwoWeekRange", "N/A")
        dividend = data.get("dividendYield", None)
        pe_ratio = data.get("forwardPE", None)
        industry = data.get("industry", "N/A")
        sector = data.get("sector", "N/A")
        exchange = data.get("fullExchangeName", "N/A")
        long_summary = data.get("longBusinessSummary", "N/A")
        year_founded = "N/A"
        fund_inception_date = data.get("fundInceptionDate", None)

        if industry == "N/A":
            industry = data.get("category", "N/A")

        if sector == "N/A":
            sector = data.get("legalType", "N/A")

        if "was founded " in long_summary or "was established " or "was incorporated ":
            year_founded_text = re.sub(r".*?(?:founded|incorporated|established) (?:in|on) (\d+).+?$", r"\1", long_summary)

            if year_founded_text != long_summary:
                year_founded = int(year_founded_text)

        if not isinstance(year_founded, int) and fund_inception_date:
            year_founded = int(datetime.utcfromtimestamp(fund_inception_date).strftime('%Y'))

        after_hours_change_percent = data.get("postMarketChangePercent", 0.0)

        if after_hours_change_percent is None:
            after_hours_change_percent = data.get("preMarketChangePercent", 0.0)

        after_hours_change_percent_symbol = ''
        after_hours_change_format_start = ''
        after_hours_change_format_end = ''

        if after_hours_change_percent > 0:
            after_hours_change_percent_symbol = '+'
            after_hours_change_format_start = "\x033"
            after_hours_change_format_end = "\x0F"
        elif after_hours_change_percent < 0:
            after_hours_change_format_start = "\x034"
            after_hours_change_format_end = "\x0F"

        absolute_change_symbol = ''
        absolute_change_format_start = ''
        absolute_change_format_end = ''

        if absolute_change > 0:
            absolute_change_symbol = '+'
            absolute_change_format_start = "\x033"
            absolute_change_format_end = "\x0F"
        elif absolute_change < 0:
            absolute_change_format_start = "\x034"
            absolute_change_format_end = "\x0F"

        relative_change_percent_symbol = ''
        relative_change_format_start = ''
        relative_change_format_end = ''

        if relative_change > 0:
            relative_change_percent_symbol = '+'
            relative_change_format_start = "\x033"
            relative_change_format_end = "\x0F"
        elif relative_change < 0:
            relative_change_format_start = "\x034"
            relative_change_format_end = "\x0F"

        message = f"{ticker}: {format(price, '.2f')} {absolute_change_format_start}{absolute_change_symbol}{format(absolute_change, '.2f')}{absolute_change_format_end} {relative_change_format_start}{relative_change_percent_symbol}{format(relative_change, '.2f')}%{relative_change_format_end} AH: {after_hours_change_format_start}{after_hours_change_percent_symbol}{format(after_hours_change_percent, '.2f')}%{after_hours_change_format_end} | {stock_name} (Industry: {industry}) (Sector: {sector}) (Exchange: {exchange}) | Div: {dividend} | P/E: {pe_ratio} | MCap: {self.format_number(market_cap)} | 52WR: {fifty_two_week_range} | V: {volume} | Year Founded: {year_founded}"
        connection.privmsg(channel, message)

    def handle_stock_info(self, connection, sender, message, channel):
        """Handle .t command."""

        import yfinance as yf

        ticker = re.sub(r"^\.t ", "", message)
        ticker = re.sub(r"^ ", "", ticker)

        if re.match("^\$", ticker):
            ticker = re.sub(r"^\$", "", ticker)

        stock = yf.Ticker(ticker)

        data = stock.info
        summary = data.get("longBusinessSummary")

        message = summary.strip()
        message_2 = ''
        message_3 = ''
        message_4 = ''
        message_5 = ''

        if len(message.encode('utf-8')) > 451:
            message_2 = message[450:]
            message_2 = message_2.strip()

            if re.match(r"[-\. ,]", message[-1]) and not re.match(r"[,]", message_2[0]):
                message = message[:450] + '-'
            else:
                message = message[:450]

        if len(message_2.encode('utf-8')) > 451:
            message_3 = message_2[450:]
            message_3 = message_3.strip()

            if re.match(r"[-\. !,]", message_2[-1]) and not re.match(r"[,]", message_3[0]):
                message_2 = message_2[:450] + '-'
            else:
                message_2 = message_2[:450]

        if len(message_3.encode('utf-8')) > 451:
            message_4 = message_3[450:]
            message_4 = message_4.strip()

            if re.match(r"[-\. !],", message_3[-1]) and not re.match(r"[,]", message_4[0]):
                message_3 = message_3[:450] + '-'
            else:
                message_3 = message_3[:450]

        if len(message_4.encode('utf-8')) > 451:
            message_5 = message_4[450:]
            message_5 = message_5.strip()

            if re.match(r"[-\. ,]", message_4[-1]) and not re.match(r"[,]", message_5[0]):
                message_4 = message_4[:450] + '-'
            else:
                message_4 = message_4[:450]

        connection.privmsg(channel, message)

        if len('message_2') > 0:
            connection.privmsg(channel, message_2)

        if len('message_3') > 0:
            connection.privmsg(channel, message_3)

        if len('message_4') > 0:
            connection.privmsg(channel, message_4)

        if len('message_5') > 0:
            connection.privmsg(channel, message_5)

    def handle_conversion(self, connection, sender, message, channel):
        """Handle !convert command."""
        currencies = re.sub(r"^!convert ", "", message)
        currencies = re.sub(r"^ ", "", currencies)
        currencies = currencies.split(" ")
        first_currency_pair = currencies[0]
        second_currency_pair = currencies[1]
        access_key = "84e55ee9fe7710f6c1ee7eff" # exchangerate-api.com
        api_base = f"https://v6.exchangerate-api.com/v6/{access_key}/latest/{first_currency_pair}"

        try:
            response = requests.get(api_base)

            if response.status_code == 200:
                data = response.json()

                if data['result'] == 'success':
                    exchange_rate = data['conversion_rates'][second_currency_pair]
                    connection.privmsg(channel, f"The exchange rate for {first_currency_pair} to {second_currency_pair} is {exchange_rate}")
                else:
                    connection.privmsg(channel, f"Unable to convert {first_currency_pair} to {second_currency_pair}")
            else:
                connection.privmsg(channel, f"Error: {response.status_code} from fixer.io")
        except Exception as e:
            logger.debug(f"Exception fetching exchange rates: {str(e)}")

    def add_command(self, command, handler):
        """Add a new command handler."""
        self.command_handlers[command] = handler
        logger.info(f"Added command: {command}")

    def create_ssl_wrapper(self):
        """Create a callable SSL wrapper for the connection."""
        ssl_context = ssl.create_default_context()
        # Configure SSL context (e.g., verify server certificate)
        ssl_context.check_hostname = True
        ssl_context.verify_mode = ssl.CERT_REQUIRED

        def ssl_wrapper(sock):
            return ssl_context.wrap_socket(sock, server_hostname=self.server)
        return ssl_wrapper

    def connect(self):
        """Connect to the IRC server with SASL and SSL."""
        try:
            # Connect with SSL and SASL
            self.connection.connect(
                server=self.server,
                port=self.port,
                nickname=self.nickname,
                password=self.sasl_password,
                username=self.sasl_username,
                ircname=self.nickname,
                connect_factory=irc.connection.Factory(wrapper=self.create_ssl_wrapper()),
                sasl_login=self.sasl_username,
            )
            logger.info(f"Attempting connection to {self.server}:{self.port}")
        except socket.gaierror as e:
            logger.error(f"Connection failed: DNS resolution error - {e}")
            raise
        except ssl.SSLError as e:
            logger.error(f"Connection failed: SSL error - {e}")
            raise
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            raise

def main():
    # Configuration

    config_file_path = os.path.join(os.getcwd(), 'config.json')

    if not os.path.exists(config_file_path):
        print(f"Please ensure {config_file_path} exists")
        sys.exit(1)

    config_file = open(config_file_path)
    config = json.load(config_file)

    # Create and start the bot
    try:
        print("Starting bot")  # Debug to confirm script start
        bot = IRCBot(config)
        bot.connect()
        bot.start()
    except KeyboardInterrupt:
        logger.info("Bot disconnected")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")

if __name__ == "__main__":
    main()
