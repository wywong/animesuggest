from bs4 import BeautifulSoup
from enum import IntEnum
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from scipy import stats
import cherrypy
import json
import numpy as np
import re
import redis
import requests
import time

redis_conn = redis.Redis(host='localhost', port=6379, db=0)

MAX_USERNAME_LENGTH = 32
MAL_PAGE_SIZE = 300
PROTOCOL = 'https://'
MAL_BASE_URL = PROTOCOL + "myanimelist.net"
AL_URL = "/animelist/%s/load.json?status=7&offset=%d"
RECOMMENDATION_SEGMENT = "/userrecs"

DEFAULT_SEED_ANIME_NUMBER = 1

MAL_UNRATED_SCORE = 0
ANIME_SCORE_KEY = "score"
ANIME_URL_KEY = "anime_url"
ANIME_TITLE_KEY = "anime_title"
ANIME_STATUS_KEY = "status"
ANIME_LIST_AFFINITY_KEY = 'affinity'
MAL_USER_KEY = 'user'


class AnimeStatus(IntEnum):
    WATCHING = 1
    COMPLETED = 2
    ON_HOLD = 3
    DROPPED = 4
    PLAN_TO_WATCH = 6
    ALL = 7


class AnimeList:
    def __init__(self, user, anime_pages):
        self.user = user
        self.excluded_anime_urls = set()
        self.completed_animes_score_desc = []
        self.rated_animes = {}

        for animes in anime_pages:
            for anime in animes:
                if self.is_excluded(anime):
                    self.excluded_anime_urls.add(anime[ANIME_URL_KEY])
                if anime[ANIME_STATUS_KEY] == AnimeStatus.COMPLETED:
                    self.completed_animes_score_desc.append(anime)
                score = anime[ANIME_SCORE_KEY]
                if score != MAL_UNRATED_SCORE:
                    key = anime[ANIME_URL_KEY]
                    self.rated_animes[key] = score
        self.completed_animes_score_desc.sort(
            key=lambda x: x[ANIME_SCORE_KEY], reverse=True
        )

    def is_excluded(self, anime):
        status = anime[ANIME_STATUS_KEY]
        return status == AnimeStatus.WATCHING or \
            status == AnimeStatus.COMPLETED or \
            status == AnimeStatus.DROPPED

    def seed_anime_urls(self):
        seed_anime = \
            self.completed_animes_score_desc[:DEFAULT_SEED_ANIME_NUMBER]
        return list(map(lambda anime: anime[ANIME_URL_KEY], seed_anime))


class AnimeListFetcher:

    def wrapped_request(self, url):
        s = requests.Session()
        retries = Retry(
            total=5, backoff_factor=5, status_forcelist=[502, 503, 504]
        )
        s.mount(PROTOCOL, HTTPAdapter(max_retries=retries))
        response = s.get(url)
        return response

    def animelist(self, username):
        key = 'al:' + username
        anime_pages_json = redis_conn.get(key)
        if anime_pages_json is None:
            anime_pages = self.fetch_animes(username)
            redis_conn.set(key, json.dumps(anime_pages))
        else:
            anime_pages = json.loads(anime_pages_json)
        return AnimeList(username, anime_pages)

    def fetch_animes(self, username):
        page = 0
        anime_pages = []
        while True:
            offset = page * MAL_PAGE_SIZE
            list_url = MAL_BASE_URL + AL_URL % (username, offset)
            response = self.wrapped_request(list_url)
            if response.status_code == 200:
                animes = response.json()
                anime_pages.append(animes)
                if len(animes) != MAL_PAGE_SIZE:
                    break
                page += 1
                time.sleep(3)
            else:
                print("Failed to get all of: %s" % username)
                time.sleep(10)
                break
        return anime_pages

    def recommendations(self, anime_url):
        recommendation_url = MAL_BASE_URL + anime_url + RECOMMENDATION_SEGMENT
        key = 'userrec:' + anime_url
        cached_html = redis_conn.get(key)
        if cached_html is None:
            time.sleep(3)
            response = self.wrapped_request(recommendation_url)
            if response.status_code == 200:
                redis_conn.set(key, response.text)
                return self.parse_recommendation_html(response.text)
            else:
                time.sleep(10)
                return []
        else:
            return self.parse_recommendation_html(cached_html)

    def parse_recommendation_html(self, html):
        soup = BeautifulSoup(html, "html.parser")
        marker_node = soup.find('div', {'id': 'horiznav_nav'})
        nodes = marker_node.find_next_siblings(
            'div', {'class': 'borderClass'}
        )
        return list(map(self.extract_node_info, nodes))

    def extract_node_info(self, node):
        parent = node.find('div', {'class': 'picSurround'})
        atag = parent.find('a', {'class': 'hoverinfo_trigger'})
        anime_url = atag['href']
        anime_title = atag.find('img')['alt']
        all_atags = node.find_all('a')
        recommender_atag = next(a for a in all_atags if 'profile' in a['href'])
        recommender = re.sub('^/profile/', '', recommender_atag['href'])
        return {
            ANIME_TITLE_KEY: anime_title,
            ANIME_URL_KEY: re.sub(MAL_BASE_URL, '', anime_url),
            MAL_USER_KEY: recommender
        }


class SuggestAnime:
    def __init__(self, fetcher):
        self.fetcher = fetcher

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def suggest(self, user=""):
        self.validateUsername(user)
        animelist = self.fetcher.animelist(user)
        seed_anime_urls = animelist.seed_anime_urls()
        recommendations = self.process_recommendations(
            animelist, seed_anime_urls
        )
        return {
            "recommendations": recommendations
        }

    def validateUsername(self, user):
        if user == "":
            raise ValueError("Empty username")
        if len(user) > MAX_USERNAME_LENGTH:
            raise ValueError("Username too long")
        if re.match('^[\w-]*$', user) is None:
            raise ValueError("Username contains invalid character")

    def process_recommendations(self, animelist, anime_urls):
        recommendations = []
        recommended_set = set()
        excluded_animes = animelist.excluded_anime_urls
        for url in anime_urls:
            raw_recommendations = self.fetcher.recommendations(url)
            for rec in raw_recommendations:
                rec_url = rec[ANIME_URL_KEY]
                is_excluded_anime = rec_url in excluded_animes
                already_recommended = rec_url in recommended_set
                if not (is_excluded_anime or already_recommended):
                    recommender = rec[MAL_USER_KEY]
                    score = self.compute_recommendation_score(
                        animelist, recommender
                    )
                    rec[ANIME_LIST_AFFINITY_KEY] = score
                    recommendations.append(rec)
                    recommended_set.add(rec_url)

        recommendations.sort(
            key=lambda x: x[ANIME_LIST_AFFINITY_KEY], reverse=True
        )
        return recommendations

    def compute_recommendation_score(self, recommendee_list, recommender):
        ratings_a = recommendee_list.rated_animes
        ratings_b = self.fetcher.animelist(recommender).rated_animes
        keys_a = set(ratings_a)
        keys_b = set(ratings_b)
        intersecting_keys = keys_a & keys_b
        a = []
        b = []
        for key in intersecting_keys:
            a.append(ratings_a[key])
            b.append(ratings_b[key])

        pearson_coef, p_value = stats.pearsonr(a, b)
        if np.isnan(pearson_coef):
            return 0
        else:
            return pearson_coef * 100


anime_fetcher = AnimeListFetcher()

if __name__ == '__main__':
    cherrypy.quickstart(SuggestAnime(anime_fetcher))
