from concurrent.futures import ThreadPoolExecutor
from time import sleep

from os.path import sep as pathSep
from os import makedirs, system as execute
from pathlib import Path
from shlex import quote
import errno

import logging
from tempfile import gettempdir

import requests
from requests import get

from urllib3.exceptions import SSLError as u3SSLError

from mutagen.flac import FLACNoHeaderError, error as FLACError

from deezer import TrackFormats
from deezer.errors import WrongLicense, WrongGeolocation
from deemix.types.DownloadObjects import Single, Collection
from deemix.types.Track import Track
from deemix.types.Picture import StaticPicture
from deemix.utils import USER_AGENT_HEADER
from deemix.utils.pathtemplates import generatePath, generateAlbumName, generateArtistName, generateDownloadObjectName
from deemix.tagger import tagID3, tagFLAC
from deemix.decryption import generateCryptedStreamURL, streamTrack
from deemix.settings import OverwriteOption
from deemix.errors import DownloadFailed, MD5NotFound, DownloadCanceled, PreferredBitrateNotFound, TrackNot360, AlbumDoesntExists, DownloadError, ErrorMessages

logger = logging.getLogger('deemix')

extensions = {
    TrackFormats.FLAC:    '.flac',
    TrackFormats.LOCAL:   '.mp3',
    TrackFormats.MP3_320: '.mp3',
    TrackFormats.MP3_128: '.mp3',
    TrackFormats.DEFAULT: '.mp3',
    TrackFormats.MP4_RA3: '.mp4',
    TrackFormats.MP4_RA2: '.mp4',
    TrackFormats.MP4_RA1: '.mp4'
}

formatsName = {
    TrackFormats.FLAC:    'FLAC',
    TrackFormats.LOCAL:   'MP3_MISC',
    TrackFormats.MP3_320: 'MP3_320',
    TrackFormats.MP3_128: 'MP3_128',
    TrackFormats.DEFAULT: 'MP3_MISC',
    TrackFormats.MP4_RA3: 'MP4_RA3',
    TrackFormats.MP4_RA2: 'MP4_RA2',
    TrackFormats.MP4_RA1: 'MP4_RA1'
}

TEMPDIR = Path(gettempdir()) / 'deemix-imgs'
if not TEMPDIR.is_dir(): makedirs(TEMPDIR)

def downloadImage(url, path, overwrite=OverwriteOption.DONT_OVERWRITE):
    if path.is_file() and overwrite not in [OverwriteOption.OVERWRITE, OverwriteOption.ONLY_TAGS, OverwriteOption.KEEP_BOTH]: return path

    try:
        image = get(url, headers={'User-Agent': USER_AGENT_HEADER}, timeout=30)
        image.raise_for_status()
        with open(path, 'wb') as f:
            f.write(image.content)
        return path
    except requests.exceptions.HTTPError:
        if path.is_file(): path.unlink()
        if 'cdns-images.dzcdn.net' in url:
            urlBase = url[:url.rfind("/")+1]
            pictureUrl = url[len(urlBase):]
            pictureSize = int(pictureUrl[:pictureUrl.find("x")])
            if pictureSize > 1200:
                return downloadImage(urlBase+pictureUrl.replace(f"{pictureSize}x{pictureSize}", '1200x1200'), path, overwrite)
    except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError, u3SSLError) as e:
        if path.is_file(): path.unlink()
        sleep(5)
        return downloadImage(url, path, overwrite)
    except OSError as e:
        if path.is_file(): path.unlink()
        if e.errno == errno.ENOSPC: raise DownloadFailed("noSpaceLeft") from e
        logger.exception("Error while downloading an image, you should report this to the developers: %s", e)
    return None

def getPreferredBitrate(dz, track, preferredBitrate, shouldFallback, uuid=None, listener=None):
    preferredBitrate = int(preferredBitrate)
    if track.local: return TrackFormats.LOCAL

    falledBack = False
    hasAlternative = track.fallbackID != "0"
    isGeolocked = False
    wrongLicense = False

    formats_non_360 = {
        TrackFormats.FLAC: "FLAC",
        TrackFormats.MP3_320: "MP3_320",
        TrackFormats.MP3_128: "MP3_128",
    }
    formats_360 = {
        TrackFormats.MP4_RA3: "MP4_RA3",
        TrackFormats.MP4_RA2: "MP4_RA2",
        TrackFormats.MP4_RA1: "MP4_RA1",
    }

    is360format = preferredBitrate in formats_360.keys()
    if not shouldFallback:
        formats = formats_360
        formats.update(formats_non_360)
    elif is360format:
        formats = formats_360
    else:
        formats = formats_non_360

    def testURL(track, url, formatName):
        if not url: return False
        request = requests.head(
            url,
            headers={'User-Agent': USER_AGENT_HEADER},
            timeout=30
        )
        try:
            request.raise_for_status()
            track.filesizes[f"FILESIZE_{formatName}"] = int(request.headers["Content-Length"])
            track.filesizes[f"FILESIZE_{formatName}_TESTED"] = True
            return track.filesizes[f"FILESIZE_{formatName}"] != 0
        except requests.exceptions.HTTPError: # if the format is not available, Deezer returns a 403 error
            return False

    def getCorrectURL(track, formatName, formatNumber):
        nonlocal wrongLicense, isGeolocked
        url = None
        # Check the track with the legit method
        try:
            url = dz.get_track_url(track.trackToken, formatName)
            if testURL(track, url, formatName): return url
            url = None
        except (WrongLicense, WrongGeolocation) as e:
            wrongLicense = isinstance(e, WrongLicense)
            isGeolocked = isinstance(e, WrongGeolocation)
        # Fallback to old method
        if not url:
            url = generateCryptedStreamURL(track.id, track.MD5, track.mediaVersion, formatNumber)
            if testURL(track, url, formatName): return url
            url = None
        return url

    for formatNumber, formatName in formats.items():
        # Current bitrate is higher than preferred bitrate; skip
        if formatNumber > preferredBitrate: continue

        currentTrack = track
        url = getCorrectURL(currentTrack, formatName, formatNumber)
        newTrack = None
        while True:
            if not url and hasAlternative:
                newTrack = dz.gw.get_track_with_fallback(currentTrack.fallbackID)
                currentTrack = Track()
                currentTrack.parseEssentialData(newTrack)
                hasAlternative = currentTrack.fallbackID != "0"
            if not url: getCorrectURL(currentTrack, formatName, formatNumber)
            if (url or not hasAlternative): break

        if url:
            if newTrack: track.parseEssentialData(newTrack)
            track.urls[formatName] = url
            return formatNumber

        if not shouldFallback:
            if wrongLicense: raise WrongLicense(formatName)
            if isGeolocked: raise WrongGeolocation(dz.current_user['country'])
            raise PreferredBitrateNotFound
        if not falledBack:
            falledBack = True
            logger.info("%s Fallback to lower bitrate", f"[{track.mainArtist.name} - {track.title}]")
            if listener and uuid:
                listener.send('downloadInfo', {
                    'uuid': uuid,
                    'state': 'bitrateFallback',
                    'data': {
                        'id': track.id,
                        'title': track.title,
                        'artist': track.mainArtist.name
                    },
                })
    if is360format: raise TrackNot360
    return TrackFormats.DEFAULT

class Downloader:
    def __init__(self, dz, downloadObject, settings, listener=None):
        self.dz = dz
        self.downloadObject = downloadObject
        self.settings = settings
        self.bitrate = downloadObject.bitrate
        self.listener = listener

        self.playlistCoverName = None
        self.playlistURLs = []

    def start(self):
        if not self.downloadObject.isCanceled:
            if isinstance(self.downloadObject, Single):
                track = self.downloadWrapper({
                    'trackAPI_gw': self.downloadObject.single['trackAPI_gw'],
                    'trackAPI': self.downloadObject.single.get('trackAPI'),
                    'albumAPI': self.downloadObject.single.get('albumAPI')
                })
                if track: self.afterDownloadSingle(track)
            elif isinstance(self.downloadObject, Collection):
                tracks = [None] * len(self.downloadObject.collection['tracks_gw'])
                with ThreadPoolExecutor(self.settings['queueConcurrency']) as executor:
                    for pos, track in enumerate(self.downloadObject.collection['tracks_gw'], start=0):
                        tracks[pos] = executor.submit(self.downloadWrapper, {
                            'trackAPI_gw': track,
                            'albumAPI': self.downloadObject.collection.get('albumAPI'),
                            'playlistAPI': self.downloadObject.collection.get('playlistAPI')
                        })
                self.afterDownloadCollection(tracks)

        if self.listener:
            if self.downloadObject.isCanceled:
                self.listener.send('currentItemCancelled', self.downloadObject.uuid)
                self.listener.send("removedFromQueue", self.downloadObject.uuid)
            else:
                self.listener.send("finishDownload", self.downloadObject.uuid)

    def log(self, data, state):
        if self.listener:
            self.listener.send('downloadInfo', {'uuid': self.downloadObject.uuid, 'data': data, 'state': state})

    def warn(self, data, state, solution):
        if self.listener:
            self.listener.send('downloadWarn', {'uuid': self.downloadObject.uuid, 'data': data, 'state': state, 'solution': solution})

    def download(self, extraData, track=None):
        returnData = {}
        trackAPI_gw = extraData['trackAPI_gw']
        trackAPI = extraData.get('trackAPI')
        albumAPI = extraData.get('albumAPI')
        playlistAPI = extraData.get('playlistAPI')
        if self.downloadObject.isCanceled: raise DownloadCanceled
        if trackAPI_gw['SNG_ID'] == "0": raise DownloadFailed("notOnDeezer")

        itemData = {
            'id': trackAPI_gw['SNG_ID'],
            'title': trackAPI_gw['SNG_TITLE'].strip(),
            'artist': trackAPI_gw['ART_NAME']
        }

        # Create Track object
        if not track:
            self.log(itemData, "getTags")
            try:
                track = Track().parseData(
                    dz=self.dz,
                    trackAPI_gw=trackAPI_gw,
                    trackAPI=trackAPI,
                    albumAPI=albumAPI,
                    playlistAPI=playlistAPI
                )
            except AlbumDoesntExists as e:
                raise DownloadError('albumDoesntExists') from e
            except MD5NotFound as e:
                raise DownloadError('notLoggedIn') from e
            self.log(itemData, "gotTags")

        itemData = {
            'id': track.id,
            'title': track.title,
            'artist': track.mainArtist.name
        }

        # Check if track not yet encoded
        if track.MD5 == '': raise DownloadFailed("notEncoded", track)

        # Choose the target bitrate
        self.log(itemData, "getBitrate")
        try:
            selectedFormat = getPreferredBitrate(
                self.dz,
                track,
                self.bitrate,
                self.settings['fallbackBitrate'],
                self.downloadObject.uuid, self.listener
            )
        except WrongLicense as e:
            raise DownloadFailed("wrongLicense") from e
        except WrongGeolocation as e:
            raise DownloadFailed("wrongGeolocation") from e
        except PreferredBitrateNotFound as e:
            raise DownloadFailed("wrongBitrate", track) from e
        except TrackNot360 as e:
            raise DownloadFailed("no360RA") from e
        track.bitrate = selectedFormat
        track.album.bitrate = selectedFormat
        self.log(itemData, "gotBitrate")

        # Apply settings
        track.applySettings(self.settings)

        # Generate filename and filepath from metadata
        (filename, filepath, artistPath, coverPath, extrasPath) = generatePath(track, self.downloadObject, self.settings)

        # Make sure the filepath exists
        makedirs(filepath, exist_ok=True)
        extension = extensions[track.bitrate]
        writepath = filepath / f"{filename}{extension}"

        # Save extrasPath
        if extrasPath and not self.downloadObject.extrasPath: self.downloadObject.extrasPath = extrasPath

        # Generate covers URLs
        embeddedImageFormat = f'jpg-{self.settings["jpegImageQuality"]}'
        if self.settings['embeddedArtworkPNG']: embeddedImageFormat = 'png'

        track.album.embeddedCoverURL = track.album.pic.getURL(self.settings['embeddedArtworkSize'], embeddedImageFormat)
        ext = track.album.embeddedCoverURL[-4:]
        if ext[0] != ".": ext = ".jpg" # Check for Spotify images
        track.album.embeddedCoverPath = TEMPDIR / ((f"pl{track.playlist.id}" if track.album.isPlaylist else f"alb{track.album.id}") + f"_{self.settings['embeddedArtworkSize']}{ext}")

        # Download and cache coverart
        self.log(itemData, "getAlbumArt")
        track.album.embeddedCoverPath = downloadImage(track.album.embeddedCoverURL, track.album.embeddedCoverPath)
        self.log(itemData, "gotAlbumArt")

        # Save local album art
        if coverPath:
            returnData['albumURLs'] = []
            for pic_format in self.settings['localArtworkFormat'].split(","):
                if pic_format in ["png","jpg"]:
                    extendedFormat = pic_format
                    if extendedFormat == "jpg": extendedFormat += f"-{self.settings['jpegImageQuality']}"
                    url = track.album.pic.getURL(self.settings['localArtworkSize'], extendedFormat)
                    # Skip non deezer pictures at the wrong format
                    if isinstance(track.album.pic, StaticPicture) and pic_format != "jpg":
                        continue
                    returnData['albumURLs'].append({'url': url, 'ext': pic_format})
            returnData['albumPath'] = coverPath
            returnData['albumFilename'] = generateAlbumName(self.settings['coverImageTemplate'], track.album, self.settings, track.playlist)

        # Save artist art
        if artistPath:
            returnData['artistURLs'] = []
            for pic_format in self.settings['localArtworkFormat'].split(","):
                # Deezer doesn't support png artist images
                if pic_format == "jpg":
                    extendedFormat = f"{pic_format}-{self.settings['jpegImageQuality']}"
                    url = track.album.mainArtist.pic.getURL(self.settings['localArtworkSize'], extendedFormat)
                    if track.album.mainArtist.pic.md5 == "": continue
                    returnData['artistURLs'].append({'url': url, 'ext': pic_format})
            returnData['artistPath'] = artistPath
            returnData['artistFilename'] = generateArtistName(self.settings['artistImageTemplate'], track.album.mainArtist, self.settings, rootArtist=track.album.rootArtist)

        # Save playlist art
        if track.playlist:
            if len(self.playlistURLs) == 0:
                for pic_format in self.settings['localArtworkFormat'].split(","):
                    if pic_format in ["png","jpg"]:
                        extendedFormat = pic_format
                        if extendedFormat == "jpg": extendedFormat += f"-{self.settings['jpegImageQuality']}"
                        url = track.playlist.pic.getURL(self.settings['localArtworkSize'], extendedFormat)
                        if isinstance(track.playlist.pic, StaticPicture) and pic_format != "jpg": continue
                        self.playlistURLs.append({'url': url, 'ext': pic_format})
            if not self.playlistCoverName:
                track.playlist.bitrate = selectedFormat
                track.playlist.dateString = track.playlist.date.format(self.settings['dateFormat'])
                self.playlistCoverName = generateAlbumName(self.settings['coverImageTemplate'], track.playlist, self.settings, track.playlist)

        # Save lyrics in lrc file
        if self.settings['syncedLyrics'] and track.lyrics.sync:
            if not (filepath / f"{filename}.lrc").is_file() or self.settings['overwriteFile'] in [OverwriteOption.OVERWRITE, OverwriteOption.ONLY_TAGS]:
                with open(filepath / f"{filename}.lrc", 'wb') as f:
                    f.write(track.lyrics.sync.encode('utf-8'))

        # Check for overwrite settings
        trackAlreadyDownloaded = writepath.is_file()

        # Don't overwrite and don't mind extension
        if not trackAlreadyDownloaded and self.settings['overwriteFile'] == OverwriteOption.DONT_CHECK_EXT:
            exts = ['.mp3', '.flac', '.opus', '.m4a']
            baseFilename = str(filepath / filename)
            for ext in exts:
                trackAlreadyDownloaded = Path(baseFilename+ext).is_file()
                if trackAlreadyDownloaded: break
        # Don't overwrite and keep both files
        if trackAlreadyDownloaded and self.settings['overwriteFile'] == OverwriteOption.KEEP_BOTH:
            baseFilename = str(filepath / filename)
            c = 1
            currentFilename = baseFilename+' ('+str(c)+')'+ extension
            while Path(currentFilename).is_file():
                c += 1
                currentFilename = baseFilename+' ('+str(c)+')'+ extension
            trackAlreadyDownloaded = False
            writepath = Path(currentFilename)

        if not trackAlreadyDownloaded or self.settings['overwriteFile'] == OverwriteOption.OVERWRITE:
            track.downloadURL = track.urls[formatsName[track.bitrate]]
            try:
                with open(writepath, 'wb') as stream:
                    streamTrack(stream, track, downloadObject=self.downloadObject, listener=self.listener)
            except requests.exceptions.HTTPError as e:
                if writepath.is_file(): writepath.unlink()
                raise DownloadFailed('notAvailable', track) from e
            except OSError as e:
                if writepath.is_file(): writepath.unlink()
                if e.errno == errno.ENOSPC: raise DownloadFailed("noSpaceLeft") from e
                raise e
            self.log(itemData, "downloaded")
        else:
            self.log(itemData, "alreadyDownloaded")
            self.downloadObject.completeTrackProgress(self.listener)

        # Adding tags
        if (not trackAlreadyDownloaded or self.settings['overwriteFile'] in [OverwriteOption.ONLY_TAGS, OverwriteOption.OVERWRITE]) and not track.local:
            self.log(itemData, "tagging")
            if extension == '.mp3':
                tagID3(writepath, track, self.settings['tags'])
            elif extension == '.flac':
                try:
                    tagFLAC(writepath, track, self.settings['tags'])
                except (FLACNoHeaderError, FLACError):
                    writepath.unlink()
                    logger.warning("%s Track not available in FLAC, falling back if necessary", f"{itemData['artist']} - {itemData['title']}")
                    self.downloadObject.removeTrackProgress(self.listener)
                    track.filesizes['FILESIZE_FLAC'] = "0"
                    track.filesizes['FILESIZE_FLAC_TESTED'] = True
                    return self.download(trackAPI_gw, track=track)
            self.log(itemData, "tagged")

        if track.searched: returnData['searched'] = True
        self.downloadObject.downloaded += 1
        self.downloadObject.files.append(str(writepath))
        if self.listener: self.listener.send("updateQueue", {
            'uuid': self.downloadObject.uuid,
            'downloaded': True,
            'downloadPath': str(writepath),
            'extrasPath': str(self.downloadObject.extrasPath)
        })
        returnData['filename'] = str(writepath)[len(str(extrasPath))+ len(pathSep):]
        returnData['data'] = itemData
        return returnData

    def downloadWrapper(self, extraData, track=None):
        trackAPI_gw = extraData['trackAPI_gw']
        if trackAPI_gw.get('_EXTRA_TRACK'):
            extraData['trackAPI'] = trackAPI_gw['_EXTRA_TRACK'].copy()
            del extraData['trackAPI_gw']['_EXTRA_TRACK']
        # Temp metadata to generate logs
        itemData = {
            'id': trackAPI_gw['SNG_ID'],
            'title': trackAPI_gw['SNG_TITLE'].strip(),
            'artist': trackAPI_gw['ART_NAME']
        }
        if trackAPI_gw.get('VERSION') and trackAPI_gw['VERSION'] not in trackAPI_gw['SNG_TITLE']:
            itemData['title'] += f" {trackAPI_gw['VERSION']}".strip()

        try:
            result = self.download(extraData, track)
        except DownloadFailed as error:
            if error.track:
                track = error.track
                if track.fallbackID != "0":
                    self.warn(itemData, error.errid, 'fallback')
                    newTrack = self.dz.gw.get_track_with_fallback(track.fallbackID)
                    track.parseEssentialData(newTrack)
                    track.retriveFilesizes(self.dz)
                    return self.downloadWrapper(extraData, track)
                if not track.searched and self.settings['fallbackSearch']:
                    self.warn(itemData, error.errid, 'search')
                    searchedId = self.dz.api.get_track_id_from_metadata(track.mainArtist.name, track.title, track.album.title)
                    if searchedId != "0":
                        newTrack = self.dz.gw.get_track_with_fallback(searchedId)
                        track.parseEssentialData(newTrack)
                        track.retriveFilesizes(self.dz)
                        track.searched = True
                        self.log(itemData, "searchFallback")
                        return self.downloadWrapper(extraData, track)
                error.errid += "NoAlternative"
                error.message = ErrorMessages[error.errid]
            result = {'error': {
                'message': error.message,
                'errid': error.errid,
                'data': itemData
            }}
        except Exception as e:
            logger.exception("%s %s", f"{itemData['artist']} - {itemData['title']}", e)
            result = {'error': {
                'message': str(e),
                'data': itemData
            }}

        if 'error' in result:
            self.downloadObject.completeTrackProgress(self.listener)
            self.downloadObject.failed += 1
            self.downloadObject.errors.append(result['error'])
            if self.listener:
                error = result['error']
                self.listener.send("updateQueue", {
                    'uuid': self.downloadObject.uuid,
                    'failed': True,
                    'data': error['data'],
                    'error': error['message'],
                    'errid': error['errid'] if 'errid' in error else None
                })
        return result

    def afterDownloadSingle(self, track):
        if not self.downloadObject.extrasPath: self.downloadObject.extrasPath = Path(self.settings['downloadLocation'])

        # Save Album Cover
        if self.settings['saveArtwork'] and 'albumPath' in track:
            for image in track['albumURLs']:
                downloadImage(image['url'], track['albumPath'] / f"{track['albumFilename']}.{image['ext']}", self.settings['overwriteFile'])

        # Save Artist Artwork
        if self.settings['saveArtworkArtist'] and 'artistPath' in track:
            for image in track['artistURLs']:
                downloadImage(image['url'], track['artistPath'] / f"{track['artistFilename']}.{image['ext']}", self.settings['overwriteFile'])

        # Create searched logfile
        if self.settings['logSearched'] and 'searched' in track:
            filename = f"{track.data.artist} - {track.data.title}"
            with open(self.downloadObject.extrasPath / 'searched.txt', 'wb+') as f:
                searchedFile = f.read().decode('utf-8')
                if not filename in searchedFile:
                    if searchedFile != "": searchedFile += "\r\n"
                    searchedFile += filename + "\r\n"
                f.write(searchedFile.encode('utf-8'))

        # Execute command after download
        if self.settings['executeCommand'] != "":
            execute(self.settings['executeCommand'].replace("%folder%", quote(str(self.downloadObject.extrasPath))).replace("%filename%", quote(track['filename'])))

    def afterDownloadCollection(self, tracks):
        if not self.downloadObject.extrasPath: self.downloadObject.extrasPath = Path(self.settings['downloadLocation'])
        playlist = [None] * len(tracks)
        errors = ""
        searched = ""

        for i, track in enumerate(tracks):
            track = track.result()
            if not track: return # Check if item is cancelled

            # Log errors to file
            if track.get('error'):
                if not track['error'].get('data'): track['error']['data'] = {'id': "0", 'title': 'Unknown', 'artist': 'Unknown'}
                errors += f"{track['error']['data']['id']} | {track['error']['data']['artist']} - {track['error']['data']['title']} | {track['error']['message']}\r\n"

            # Log searched to file
            if 'searched' in track: searched += track['searched'] + "\r\n"

            # Save Album Cover
            if self.settings['saveArtwork'] and 'albumPath' in track:
                for image in track['albumURLs']:
                    downloadImage(image['url'], track['albumPath'] / f"{track['albumFilename']}.{image['ext']}", self.settings['overwriteFile'])

            # Save Artist Artwork
            if self.settings['saveArtworkArtist'] and 'artistPath' in track:
                for image in track['artistURLs']:
                    downloadImage(image['url'], track['artistPath'] / f"{track['artistFilename']}.{image['ext']}", self.settings['overwriteFile'])

            # Save filename for playlist file
            playlist[i] = track.get('filename', "")

        # Create errors logfile
        if self.settings['logErrors'] and errors != "":
            with open(self.downloadObject.extrasPath / 'errors.txt', 'wb') as f:
                f.write(errors.encode('utf-8'))

        # Create searched logfile
        if self.settings['logSearched'] and searched != "":
            with open(self.downloadObject.extrasPath / 'searched.txt', 'wb') as f:
                f.write(searched.encode('utf-8'))

        # Save Playlist Artwork
        if self.settings['saveArtwork'] and self.playlistCoverName and not self.settings['tags']['savePlaylistAsCompilation']:
            for image in self.playlistURLs:
                downloadImage(image['url'], self.downloadObject.extrasPath / f"{self.playlistCoverName}.{image['ext']}", self.settings['overwriteFile'])

        # Create M3U8 File
        if self.settings['createM3U8File']:
            filename = generateDownloadObjectName(self.settings['playlistFilenameTemplate'], self.downloadObject, self.settings) or "playlist"
            with open(self.downloadObject.extrasPath / f'{filename}.m3u8', 'wb') as f:
                for line in playlist:
                    f.write((line + "\n").encode('utf-8'))

        # Execute command after download
        if self.settings['executeCommand'] != "":
            execute(self.settings['executeCommand'].replace("%folder%", quote(str(self.downloadObject.extrasPath))))
