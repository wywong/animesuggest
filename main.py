from bs4 import BeautifulSoup
from enum import IntEnum
from urllib import request
import cherrypy
import json
import re

MAX_USERNAME_LENGTH = 32
MAL_BASE_URL = "https://myanimelist.net"
AL_URL = "/animelist/%s/load.json?status=%d&offset=0"
RECOMMENDATION_SEGMENT = "/userrecs"

MAL_UNRATED_SCORE = 0
SCORE_KEY = "score"
ANIME_URL_KEY = "anime_url"
ANIME_TITLE_KEY = "anime_title"


class AnimeStatus(IntEnum):
    WATCHING = 1
    COMPLETED = 2
    ON_HOLD = 3
    DROPPED = 4
    ALL = 7


class AnimeListFetcher:

    def animelist(self, username, status=AnimeStatus.ALL):
        list_url = MAL_BASE_URL + AL_URL % (username, status)
        with request.urlopen(list_url) as response:
            return json.loads(response.read())

    def recommendations(self, anime_url):
        recommendation_url = MAL_BASE_URL + anime_url + RECOMMENDATION_SEGMENT
        with request.urlopen(recommendation_url) as response:
            soup = BeautifulSoup(response.read(), "html.parser")
            marker_node = soup.find('div', {'id': 'horiznav_nav'})
            nodes = marker_node.find_next_siblings(
                'div', {'class': 'borderClass'}
            )
            return list(map(self.extract_node_info, nodes))

    def extract_node_info(self, node):
        atag = node.find('a', {'title': 'Permalink'})
        print(atag)
        return {
            'url': atag['href']
        }


class SuggestAnime:
    def __init__(self, fetcher):
        self.fetcher = fetcher

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def suggest(self, user=""):
        self.validateUsername(user)
        seed_anime = self.default_seed_anime(user)
        seed_anime_url = seed_anime[ANIME_URL_KEY]
        recommendations = self.fetcher.recommendations(seed_anime_url)
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

    def default_seed_anime(self, user):
        completed_anime = self.fetcher.animelist(user, AnimeStatus.COMPLETED)
        seed_anime = completed_anime[0]
        for anime in completed_anime:
            if anime[SCORE_KEY] > seed_anime[SCORE_KEY]:
                seed_anime = anime
        return seed_anime


anime_fetcher = AnimeListFetcher()

if __name__ == '__main__':
    cherrypy.quickstart(SuggestAnime(anime_fetcher))
