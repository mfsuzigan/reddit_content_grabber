import argparse
import math
import threading
import time
from urllib.parse import urlparse
from urllib.parse import parse_qs
from argparse import Namespace
from requests import RequestException
from selenium.webdriver import Chrome
import hashlib
import os

import requests
import logging

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common import TimeoutException, NoSuchElementException, ElementNotInteractableException, \
    ElementClickInterceptedException
from selenium.webdriver import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as ec

REDDIT_URL = "https://www.reddit.com"
IMAGE_FILE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.tiff', '.bmp', '.gif', '.pjpg')
WEBDRIVER_RENDER_TIMEOUT_SECONDS = 5
POST_EXPANSION_RETRY_SECONDS = 5
MAX_REQUEST_RETRIES = 5
THREADS_TO_USE = 8

args: Namespace
driver: Chrome
stored_content_hashes = []
master_content_map = {}
image_output_dir = None
video_output_dir = None
files_saved_counter = 0


def get_args():
    logging.info("Reading args")
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--username", "-u", required=True, help="User's username")
    arg_parser.add_argument("--password", "-p", required=True, help="User's password")
    arg_parser.add_argument("--target", "-t", required=False, help="Target profile")
    arg_parser.add_argument("--sub", "-s", required=False, help="Target subreddit")
    arg_parser.add_argument("--output", "-o", required=True, help="Output directory")
    arg_parser.add_argument("--headless", "-hl", action="store_true", help="Headless run")
    arg_parser.add_argument("--only-videos", "-v", action="store_true", help="Download only videos")
    arg_parser.add_argument("--max-files", "-m", required=False, help="Maximum number of files to download")

    return arg_parser.parse_args()


def wait_until_visible(locator):
    wait = WebDriverWait(driver, WEBDRIVER_RENDER_TIMEOUT_SECONDS)
    try:
        wait.until(ec.visibility_of_element_located(locator))
    except TimeoutException:
        pass


def login():
    logging.info("Logging in")
    driver.get(f"{REDDIT_URL}/login")
    driver.find_element(By.ID, "loginUsername").send_keys(args.username)
    driver.find_element(By.ID, "loginPassword").send_keys(args.password)
    driver.find_element(By.TAG_NAME, "button").click()
    wait_until_visible((By.ID, "USER_DROPDOWN_ID"))
    logging.info("Logged in")


def file_is_downloadable(name):
    name = name.split("?")[0]

    if args.only_videos:
        return name.endswith('.gif')

    else:
        return name.endswith(IMAGE_FILE_EXTENSIONS)


def file_is_type(name, extension):
    name = name.split("?")[0]
    return name.endswith(extension)


def file_is_image(extension):
    return extension.lower() in IMAGE_FILE_EXTENSIONS


def page_scroll(key):
    page_body = driver.find_element(By.TAG_NAME, "body")
    page_body.send_keys(key)
    direction = "down" if key == Keys.PAGE_DOWN else "up"
    logging.info(f"Page scrolled {direction}")


def store_user_content_urls():
    driver.get(f"{REDDIT_URL}/user/{args.target}/submitted")
    user_posts_xpath = "//*[@id='AppRouter-main-content']/div/div/div[2]/div[3]/div[1]/div[3]/div"
    store_content_urls(user_posts_xpath)


def store_content_urls(grid_elements_xpath, max_posts_to_inspect=None):
    logging.info("Storing content urls")

    grid_elements = driver.find_elements(By.XPATH, grid_elements_xpath)
    inspected_elements = []

    while (len(inspected_elements) != len(grid_elements) and
           (not max_posts_to_inspect or len(inspected_elements) < max_posts_to_inspect)):
        elements_to_inspect = [e for e in grid_elements if e not in inspected_elements]
        inspect_posts_for_content(elements_to_inspect)
        inspected_elements.extend(elements_to_inspect)
        logging.info(f"{len(inspected_elements)} posts inspected 🛠️️")
        grid_elements = driver.find_elements(By.XPATH, grid_elements_xpath)


def setup_output_directories():
    logging.info("Setting up directories")
    base_output_dir = f"{args.output}/{args.target if args.target else args.sub}"

    global image_output_dir
    image_output_dir = f"{base_output_dir}/img"
    os.makedirs(image_output_dir, exist_ok=True)

    global video_output_dir
    video_output_dir = f"{base_output_dir}/video"
    os.makedirs(video_output_dir, exist_ok=True)


def safely_request_content(url):
    successful = False
    content = ""

    for _ in range(MAX_REQUEST_RETRIES):

        if successful:
            break

        else:
            try:
                content = requests.get(url).content
                successful = True

            except RequestException:
                logging.warning(f"Error downloading {url}")

    return content


def store_link_from_inspectable_file(link, title=None, user=None):
    soup = BeautifulSoup(safely_request_content(link), "html.parser")
    src = soup.find("meta", {"property": "og:video"})

    if not src:
        src = soup.find("meta", {"property": "og:image:url"})

    if not src:
        return False

    if not title:
        title = soup.find("title").text if soup.find("title") else "UNKNOWN_TITLE"

    title_parts = title.split("by ")

    if not user:
        user = title_parts[1].split(" |")[0] if len(title_parts) > 1 else "UNKNOWN_USER"

    return store_link(link=src.attrs["content"], title=title, user=user)


def get_redgifs_link(post):
    redgif_link_probe = post.find_elements(By.CSS_SELECTOR, "a[href*=redgifs]")

    if len(redgif_link_probe) > 0:
        return redgif_link_probe[0].get_attribute("href")

    else:
        return None


def toggle_complex_post_details(expand_button):
    successful = False

    while not successful:
        try:
            expand_button.click()
            successful = True

        except (ElementClickInterceptedException, ElementNotInteractableException):
            logging.warning("Error expanding complex post details, retrying...")
            page_scroll(Keys.PAGE_UP)
            time.sleep(POST_EXPANSION_RETRY_SECONDS)


def centralize_at_element(element):
    driver.execute_script("arguments[0].scrollIntoView(true);", element)


def expand_posts_for_details(composite_posts):
    if args.only_videos:
        return

    for post in composite_posts:
        expand_button_probe = post.find_elements(By.CSS_SELECTOR,
                                                 "div[data-click-id='body'] button[aria-label='Expand content']")

        if len(expand_button_probe) > 0:
            logging.info("Expanding post for details")
            toggle_complex_post_details(expand_button_probe[0])
            time.sleep(2)

            post_image_elements = post.find_elements(By.CSS_SELECTOR, "img[src]:not([alt='']):not([src=''])")

            if len(post_image_elements) >= 5:
                next_button_probe = post.find_elements(By.CSS_SELECTOR, "a[title='Next']")

                while len(next_button_probe) > 0:
                    next_button_probe[0].click()
                    next_button_probe = post.find_elements(By.CSS_SELECTOR, "a[title='Next']")

                post_image_elements = post.find_elements(By.CSS_SELECTOR, "img[src]:not([alt='']):not([src=''])")

            author = get_post_author(post)

            [download_image_element(image_element=i, user=author) for i in post_image_elements]
            toggle_complex_post_details(expand_button_probe[0])


def get_post_author(post):
    try:
        return post.find_element(By.CSS_SELECTOR, "a[data-testid='post_author_link']").text.replace("u/", "")

    except NoSuchElementException:
        return "UNKNOWN"


def sanitize_string(str_input):
    return "".join(c for c in str_input if c.isalnum() or c in "._- ")


def is_duplicate(image_content):
    global stored_content_hashes
    image_hash = hashlib.md5(image_content).hexdigest()
    is_duplicated = image_hash in stored_content_hashes

    if not is_duplicated:
        stored_content_hashes.append(image_hash)

    return is_duplicated


def download_image_element(image_element, user, image_src=None, image_title=None):
    if not image_title:
        image_title = image_element.get_attribute("alt")

    if not image_src:
        image_src = image_element.get_attribute("src")

    return (file_is_downloadable(image_src.split("/")[-1]) and store_link(link=image_src, title=image_title,
                                                                          user=user))


def save_files(content_map):
    for (path, url) in content_map.items():
        save_file(path, url)


def save_file(local_path, url):
    content = safely_request_content(url)
    is_successful = False
    thread_id = threading.currentThread().ident

    if content:
        if is_duplicate(content):
            logging.info(f"T-{thread_id}: Skipping duplicate file by content: {local_path}")

        else:
            try:
                with open(local_path, "wb") as file:
                    logging.info(f"T-{thread_id}: Downloading file: {local_path.split('/')[-1]}")
                    file.write(content)
                    is_successful = True

            except OSError as e:
                logging.error(f"T-{thread_id}: Error writing file: {local_path}", e)

    return is_successful


def store_link(link, user, title):
    parsed_link = urlparse(link)
    name_parts = parsed_link.path.split(".")
    name_identifier = name_parts[0].split("/")[-1]
    format_probe = parse_qs(parsed_link.query)

    if "format" in format_probe.keys():
        extension = format_probe["format"][0]

    else:
        extension = name_parts[-1]

    path = image_output_dir if file_is_image(extension) else video_output_dir
    local_path = f"{path}/{user}__{name_identifier}__{sanitize_string(title[:30])}.{extension}"
    content: bytes
    is_successful = False

    if os.path.exists(local_path):
        logging.info(f"Skipping existing file {local_path}")

    else:
        master_content_map[local_path] = link
        is_successful = True

    return is_successful


def inspect_posts_for_content(elements_grid):
    for element in elements_grid:
        centralize_at_element(element)
        href_probe = element.find_elements(By.TAG_NAME, "a")
        post_title_probe = element.find_elements(By.TAG_NAME, "h3")
        author = get_post_author(element)

        if len(href_probe) > 0 and len(post_title_probe) > 0:
            src = href_probe[0].get_attribute("href")
            post_title = post_title_probe[0].text
            logging.info(f"Inspecting post: {post_title}")

            if file_is_type(src, "gifv"):
                store_link_from_inspectable_file(src, post_title, author)

            elif len(element.find_elements(By.CSS_SELECTOR, "a[href*=redgifs]")) > 0:
                store_link_from_inspectable_file(src)

            elif file_is_downloadable(src.split("/")[-1]):
                download_image_element(image_element=element.find_element(By.TAG_NAME, "img"),
                                       image_src=src,
                                       image_title=post_title, user=author)

            else:
                expand_posts_for_details([element])

        else:
            expand_posts_for_details([element])


def store_sub_content_urls():
    sub_posts_xpath = "//*[@id='AppRouter-main-content']/div/div/div[2]/div[4]/div[1]/div[5]/div"
    max_files = None

    if args.max_files:
        max_files = int(args.max_files)

    driver.get(f"{REDDIT_URL}/r/{args.sub}/")
    set_classic_view_mode()
    store_content_urls(sub_posts_xpath, max_files)


def download_content():
    logging.info("Downloading content")
    sub_content_map_size = math.ceil(len(master_content_map) / THREADS_TO_USE)

    while len(master_content_map) > 0:
        sub_content_map = {}

        for _ in range(sub_content_map_size):

            if len(master_content_map) > 0:
                item = master_content_map.popitem()
                sub_content_map[item[0]] = item[1]

            else:
                break

        thread = threading.Thread(target=save_files, args=(sub_content_map,))
        logging.info(f"Starting thread")
        thread.start()


def set_classic_view_mode():
    switch_button_probe = driver.find_elements(By.ID, "LayoutSwitch--picker")

    if len(switch_button_probe) > 0:
        switch_button = switch_button_probe[0]
        classic_button_probe = switch_button.find_elements(By.CSS_SELECTOR, "i[class*='classic']")

        if len(classic_button_probe) == 0:
            switch_button.click()
            classic_button_probe = driver.find_elements(By.CSS_SELECTOR,
                                                        "div[role='menu'] > button[role='menuitem'] > "
                                                        "span > i[class*='icon-view_classic']")
            if len(classic_button_probe) > 0:
                classic_button_probe[0].click()


def main():
    logging.getLogger().setLevel(logging.INFO)
    logging.info("Starting")

    global args
    args = get_args()

    webdriver_setup()
    login()
    setup_output_directories()

    if args.target:
        store_user_content_urls()

    elif args.sub:
        store_sub_content_urls()

    else:
        logging.error("Either a target user or subreddit is required")

    download_content()
    logging.info("Done")


def webdriver_setup():
    logging.info("Setting up webdriver")
    options = webdriver.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument('--disable-dev-shm-usage')

    if args.headless:
        options.add_argument("--headless")

    global driver
    driver = webdriver.Chrome(options=options)


if __name__ == "__main__":
    main()
