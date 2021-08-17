from time import sleep
import re
import requests

from deezer.errors import APIError, GWAPIError
from deemix.errors import TrackError, NoDataToParse, AlbumDoesntExists

from deemix.utils import removeFeatures, andCommaConcat, removeDuplicateArtists, generateReplayGainString, changeCase

from deemix.types.Album import Album
from deemix.types.Artist import Artist
from deemix.types.Date import Date
from deemix.types.Picture import Picture
from deemix.types.Playlist import Playlist
from deemix.types.Lyrics import Lyrics
from deemix.types import VARIOUS_ARTISTS

from deemix.settings import FeaturesOption

class Track:
    def __init__(self, sng_id="0", name=""):
        self.id = sng_id
        self.title = name
        self.MD5 = ""
        self.mediaVersion = ""
        self.trackToken = ""
        self.duration = 0
        self.fallbackID = "0"
        self.filesizes = {}
        self.local = False
        self.mainArtist = None
        self.artist = {"Main": []}
        self.artists = []
        self.album = None
        self.trackNumber = "0"
        self.discNumber = "0"
        self.date = Date()
        self.lyrics = None
        self.bpm = 0
        self.contributors = {}
        self.copyright = ""
        self.explicit = False
        self.ISRC = ""
        self.replayGain = ""
        self.playlist = None
        self.position = None
        self.searched = False
        self.selectedFormat = 0
        self.singleDownload = False
        self.dateString = ""
        self.artistsString = ""
        self.mainArtistsString = ""
        self.featArtistsString = ""
        self.urls = {}

    def parseEssentialData(self, trackAPI_gw, trackAPI=None):
        self.id = str(trackAPI_gw['SNG_ID'])
        self.duration = trackAPI_gw['DURATION']
        self.trackToken = trackAPI_gw['TRACK_TOKEN']
        self.rank = trackAPI_gw['RANK_SNG']
        self.MD5 = trackAPI_gw.get('MD5_ORIGIN')
        if not self.MD5:
            if trackAPI and trackAPI.get('md5_origin'):
                self.MD5 = trackAPI['md5_origin']
            #else:
            #    raise MD5NotFound
        self.mediaVersion = trackAPI_gw['MEDIA_VERSION']
        self.fallbackID = "0"
        if 'FALLBACK' in trackAPI_gw:
            self.fallbackID = trackAPI_gw['FALLBACK']['SNG_ID']
        self.local = int(self.id) < 0
        self.urls = {}

    def retriveFilesizes(self, dz):
        guest_sid = dz.session.cookies.get('sid')
        try:
            site = requests.post(
                "https://api.deezer.com/1.0/gateway.php",
                params={
                    'api_key': "4VCYIJUCDLOUELGD1V8WBVYBNVDYOXEWSLLZDONGBBDFVXTZJRXPR29JRLQFO6ZE",
                    'sid': guest_sid,
                    'input': '3',
                    'output': '3',
                    'method': 'song_getData'
                },
                timeout=30,
                json={'sng_id': self.id},
                headers=dz.http_headers
            )
            result_json = site.json()
        except:
            sleep(2)
            self.retriveFilesizes(dz)
        if len(result_json['error']):
            raise TrackError(result_json.dumps(result_json['error']))
        response = result_json.get("results", {})
        filesizes = {}
        for key, value in response.items():
            if key.startswith("FILESIZE_"):
                filesizes[key] = int(value)
                filesizes[key+"_TESTED"] = False
        self.filesizes = filesizes

    def parseData(self, dz, track_id=None, trackAPI_gw=None, trackAPI=None, albumAPI_gw=None, albumAPI=None, playlistAPI=None):
        if track_id and not trackAPI_gw: trackAPI_gw = dz.gw.get_track_with_fallback(track_id)
        elif not trackAPI_gw: raise NoDataToParse
        if not trackAPI:
            try: trackAPI = dz.api.get_track(trackAPI_gw['SNG_ID'])
            except APIError: trackAPI = None

        self.parseEssentialData(trackAPI_gw, trackAPI)

        if self.local:
            self.parseLocalTrackData(trackAPI_gw)
        else:
            self.retriveFilesizes(dz)
            self.parseTrackGW(trackAPI_gw)

            # Get Lyrics data
            if not "LYRICS" in trackAPI_gw and self.lyrics.id != "0":
                try: trackAPI_gw["LYRICS"] = dz.gw.get_track_lyrics(self.id)
                except GWAPIError: self.lyrics.id = "0"
            if self.lyrics.id != "0": self.lyrics.parseLyrics(trackAPI_gw["LYRICS"])

            # Parse Album Data
            self.album = Album(
                alb_id = trackAPI_gw['ALB_ID'],
                title = trackAPI_gw['ALB_TITLE'],
                pic_md5 = trackAPI_gw.get('ALB_PICTURE')
            )

            # Get album Data
            if not albumAPI:
                try: albumAPI = dz.api.get_album(self.album.id)
                except APIError: albumAPI = None

            # Get album_gw Data
            if not albumAPI_gw:
                try: albumAPI_gw = dz.gw.get_album(self.album.id)
                except GWAPIError: albumAPI_gw = None

            if albumAPI:
                self.album.parseAlbum(albumAPI)
            elif albumAPI_gw:
                self.album.parseAlbumGW(albumAPI_gw)
                # albumAPI_gw doesn't contain the artist cover
                # Getting artist image ID
                # ex: https://e-cdns-images.dzcdn.net/images/artist/f2bc007e9133c946ac3c3907ddc5d2ea/56x56-000000-80-0-0.jpg
                artistAPI = dz.api.get_artist(self.album.mainArtist.id)
                self.album.mainArtist.pic.md5 = artistAPI['picture_small'][artistAPI['picture_small'].find('artist/') + 7:-24]
            else:
                raise AlbumDoesntExists

            # Fill missing data
            if albumAPI_gw: self.album.addExtraAlbumGWData(albumAPI_gw)
            if self.album.date and not self.date: self.date = self.album.date
            if not self.album.discTotal: self.album.discTotal = albumAPI_gw.get('NUMBER_DISK', "1")
            if not self.copyright: self.copyright = albumAPI_gw['COPYRIGHT']
            self.parseTrack(trackAPI)

        # Remove unwanted charaters in track name
        # Example: track/127793
        self.title = ' '.join(self.title.split())

        # Make sure there is at least one artist
        if len(self.artist['Main']) == 0:
            self.artist['Main'] = [self.mainArtist.name]

        self.position = trackAPI_gw.get('POSITION')

        # Add playlist data if track is in a playlist
        if playlistAPI: self.playlist = Playlist(playlistAPI)

        self.generateMainFeatStrings()
        return self

    def parseLocalTrackData(self, trackAPI_gw):
        # Local tracks has only the trackAPI_gw page and
        # contains only the tags provided by the file
        self.title = trackAPI_gw['SNG_TITLE']
        self.album = Album(title=trackAPI_gw['ALB_TITLE'])
        self.album.pic = Picture(
            md5 = trackAPI_gw.get('ALB_PICTURE', ""),
            pic_type = "cover"
        )
        self.mainArtist = Artist(name=trackAPI_gw['ART_NAME'], role="Main")
        self.artists = [trackAPI_gw['ART_NAME']]
        self.artist = {
            'Main': [trackAPI_gw['ART_NAME']]
        }
        self.album.artist = self.artist
        self.album.artists = self.artists
        self.album.date = self.date
        self.album.mainArtist = self.mainArtist

    def parseTrackGW(self, trackAPI_gw):
        self.title = trackAPI_gw['SNG_TITLE'].strip()
        if trackAPI_gw.get('VERSION') and not trackAPI_gw['VERSION'].strip() in self.title:
            self.title += f" {trackAPI_gw['VERSION'].strip()}"

        self.discNumber = trackAPI_gw.get('DISK_NUMBER')
        self.explicit = bool(int(trackAPI_gw.get('EXPLICIT_LYRICS', "0")))
        self.copyright = trackAPI_gw.get('COPYRIGHT')
        if 'GAIN' in trackAPI_gw: self.replayGain = generateReplayGainString(trackAPI_gw['GAIN'])
        self.ISRC = trackAPI_gw.get('ISRC')
        self.trackNumber = trackAPI_gw['TRACK_NUMBER']
        self.contributors = trackAPI_gw['SNG_CONTRIBUTORS']

        self.lyrics = Lyrics(trackAPI_gw.get('LYRICS_ID', "0"))

        self.mainArtist = Artist(
            art_id = trackAPI_gw['ART_ID'],
            name = trackAPI_gw['ART_NAME'],
            role = "Main",
            pic_md5 = trackAPI_gw.get('ART_PICTURE')
        )

        if 'PHYSICAL_RELEASE_DATE' in trackAPI_gw:
            self.date.day = trackAPI_gw["PHYSICAL_RELEASE_DATE"][8:10]
            self.date.month = trackAPI_gw["PHYSICAL_RELEASE_DATE"][5:7]
            self.date.year = trackAPI_gw["PHYSICAL_RELEASE_DATE"][0:4]
            self.date.fixDayMonth()

    def parseTrack(self, trackAPI):
        self.bpm = trackAPI['bpm']

        if not self.replayGain and 'gain' in trackAPI:
            self.replayGain = generateReplayGainString(trackAPI['gain'])
        if not self.explicit:
            self.explicit = trackAPI['explicit_lyrics']
        if not self.discNumber:
            self.discNumber = trackAPI['disk_number']

        for artist in trackAPI['contributors']:
            isVariousArtists = str(artist['id']) == VARIOUS_ARTISTS
            isMainArtist = artist['role'] == "Main"

            if len(trackAPI['contributors']) > 1 and isVariousArtists:
                continue

            if artist['name'] not in self.artists:
                self.artists.append(artist['name'])

            if isMainArtist or artist['name'] not in self.artist['Main'] and not isMainArtist:
                if not artist['role'] in self.artist:
                    self.artist[artist['role']] = []
                self.artist[artist['role']].append(artist['name'])

    def removeDuplicateArtists(self):
        (self.artist, self.artists) = removeDuplicateArtists(self.artist, self.artists)

    # Removes featuring from the title
    def getCleanTitle(self):
        return removeFeatures(self.title)

    def getFeatTitle(self):
        if self.featArtistsString and "feat." not in self.title.lower():
            return f"{self.title} ({self.featArtistsString})"
        return self.title

    def generateMainFeatStrings(self):
        self.mainArtistsString = andCommaConcat(self.artist['Main'])
        self.featArtistsString = ""
        if 'Featured' in self.artist:
            self.featArtistsString = "feat. "+andCommaConcat(self.artist['Featured'])

    def applySettings(self, settings):

        # Check if should save the playlist as a compilation
        if self.playlist and settings['tags']['savePlaylistAsCompilation']:
            self.trackNumber = self.position
            self.discNumber = "1"
            self.album.makePlaylistCompilation(self.playlist)
        else:
            if self.album.date: self.date = self.album.date

        self.dateString = self.date.format(settings['dateFormat'])
        self.album.dateString = self.album.date.format(settings['dateFormat'])
        if self.playlist: self.playlist.dateString = self.playlist.date.format(settings['dateFormat'])

        # Check various artist option
        if settings['albumVariousArtists'] and self.album.variousArtists:
            artist = self.album.variousArtists
            isMainArtist = artist.role == "Main"

            if artist.name not in self.album.artists:
                self.album.artists.insert(0, artist.name)

            if isMainArtist or artist.name not in self.album.artist['Main'] and not isMainArtist:
                if artist.role not in self.album.artist:
                    self.album.artist[artist.role] = []
                self.album.artist[artist.role].insert(0, artist.name)
        self.album.mainArtist.save = not self.album.mainArtist.isVariousArtists() or settings['albumVariousArtists'] and self.album.mainArtist.isVariousArtists()

        # Check removeDuplicateArtists
        if settings['removeDuplicateArtists']: self.removeDuplicateArtists()

        # Check if user wants the feat in the title
        if str(settings['featuredToTitle']) == FeaturesOption.REMOVE_TITLE:
            self.title = self.getCleanTitle()
        elif str(settings['featuredToTitle']) == FeaturesOption.MOVE_TITLE:
            self.title = self.getFeatTitle()
        elif str(settings['featuredToTitle']) == FeaturesOption.REMOVE_TITLE_ALBUM:
            self.title = self.getCleanTitle()
            self.album.title = self.album.getCleanTitle()

        # Remove (Album Version) from tracks that have that
        if settings['removeAlbumVersion'] and "Album Version" in self.title:
            self.title = re.sub(r' ?\(Album Version\)', "", self.title).strip()

        # Change Title and Artists casing if needed
        if settings['titleCasing'] != "nothing":
            self.title = changeCase(self.title, settings['titleCasing'])
        if settings['artistCasing'] != "nothing":
            self.mainArtist.name = changeCase(self.mainArtist.name, settings['artistCasing'])
            for i, artist in enumerate(self.artists):
                self.artists[i] = changeCase(artist, settings['artistCasing'])
            for art_type in self.artist:
                for i, artist in enumerate(self.artist[art_type]):
                    self.artist[art_type][i] = changeCase(artist, settings['artistCasing'])
            self.generateMainFeatStrings()

        # Generate artist tag
        if settings['tags']['multiArtistSeparator'] == "default":
            if str(settings['featuredToTitle']) == FeaturesOption.MOVE_TITLE:
                self.artistsString = ", ".join(self.artist['Main'])
            else:
                self.artistsString = ", ".join(self.artists)
        elif settings['tags']['multiArtistSeparator'] == "andFeat":
            self.artistsString = self.mainArtistsString
            if self.featArtistsString and str(settings['featuredToTitle']) != FeaturesOption.MOVE_TITLE:
                self.artistsString += " " + self.featArtistsString
        else:
            separator = settings['tags']['multiArtistSeparator']
            if str(settings['featuredToTitle']) == FeaturesOption.MOVE_TITLE:
                self.artistsString = separator.join(self.artist['Main'])
            else:
                self.artistsString = separator.join(self.artists)
