from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_pascal


class JellyModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_pascal,
        populate_by_name=True,
    )


class SystemInfo(JellyModel):
    id: str
    local_address: str
    server_name: str
    version: str
    product_name: str
    operating_system: str
    startup_wizard_completed: bool


class PlayState(JellyModel):
    can_seek: bool = False
    is_paused: bool = False
    is_muted: bool = False
    repeat_mode: str = "RepeatNone"


class Capabilities(JellyModel):
    playable_media_types: list[str] = ["Audio"]
    supported_commands: list[str] = []
    supports_media_control: bool = True
    supports_persistent_identifier: bool = True


class SessionInfo(JellyModel):
    id: str
    user_id: str
    user_name: str
    server_id: str
    remote_end_point: str | None = None
    client: str = "Finamp"
    device_name: str = "Test"
    device_id: str = "test-device"
    application_version: str = "1.0.0"
    last_activity_date: str = "2024-01-01T00:00:00.0000000Z"
    last_playback_check_in: str = "2024-01-01T00:00:00.0000000Z"
    is_active: bool = True
    supports_media_control: bool = True
    supports_remote_control: bool = False
    has_custom_device_name: bool = False
    play_state: PlayState = PlayState()
    capabilities: Capabilities = Capabilities()
    additional_users: list = []
    playable_media_types: list[str] = ["Audio"]
    now_playing_queue: list = []
    now_playing_queue_full_items: list = []
    supported_commands: list[str] = []


class UserConfiguration(JellyModel):
    play_default_audio_track: bool = True
    subtitle_language_preference: str = ""
    display_missing_episodes: bool = False
    grouped_folders: list[str] = []
    subtitle_mode: str = "Default"
    display_collections_view: bool = False
    enable_local_password: bool = False
    ordered_views: list[str] = []
    latest_items_excludes: list[str] = []
    my_media_excludes: list[str] = []
    hide_played_in_latest: bool = True
    remember_audio_selections: bool = True
    remember_subtitle_selections: bool = True
    enable_next_episode_auto_play: bool = False
    cast_receiver_id: str = "F007D354"


class UserPolicy(JellyModel):
    is_administrator: bool = True
    is_hidden: bool = False
    is_disabled: bool = False
    blocked_tags: list[str] = []
    allowed_tags: list[str] = []
    enable_user_preference_access: bool = True
    access_schedules: list = []
    block_unrated_items: list[str] = []
    enable_remote_control_of_other_users: bool = False
    enable_shared_device_control: bool = True
    enable_remote_access: bool = True
    enable_live_tv_management: bool = True
    enable_live_tv_access: bool = True
    enable_media_playback: bool = True
    enable_audio_playback_transcoding: bool = True
    enable_video_playback_transcoding: bool = True
    enable_playback_remuxing: bool = True
    force_remote_source_transcoding: bool = False
    enable_content_deletion: bool = False
    enable_content_deletion_from_folders: list[str] = []
    enable_content_downloading: bool = True
    enable_sync_transcoding: bool = True
    enable_media_conversion: bool = True
    enabled_devices: list[str] = []
    enable_all_devices: bool = True
    enabled_channels: list[str] = []
    enable_all_channels: bool = True
    enabled_folders: list[str] = []
    enable_all_folders: bool = True
    invalid_login_attempt_count: int = 0
    login_attempts_before_lockout: int = -1
    max_active_sessions: int = 0
    enable_public_sharing: bool = True
    blocked_media_folders: list[str] = []
    blocked_channels: list[str] = []
    remote_client_bitrate_limit: int = 0
    authentication_provider_id: str = "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider"
    password_reset_provider_id: str = "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider"
    sync_play_access: str = "CreateAndJoinGroups"


class User(JellyModel):
    id: str
    name: str
    server_id: str
    server_name: str = "FlexProxy"
    has_password: bool = False
    has_configured_password: bool = False
    has_configured_easy_password: bool = False
    enable_auto_login: bool = True
    last_login_date: str = "2024-01-01T00:00:00.0000000Z"
    last_activity_date: str = "2024-01-01T00:00:00.0000000Z"
    configuration: UserConfiguration = UserConfiguration()
    policy: UserPolicy = UserPolicy()


class AuthResponse(JellyModel):
    user: User
    session_info: SessionInfo
    access_token: str
    server_id: str


class UserData(JellyModel):
    play_count: int = 0
    playback_position_ticks: int = 0
    is_favorite: bool = False
    played: bool = False
    # Jellyfin's per-item user-data key. Callers pass the item id; the default
    # used to be one str(uuid4()) evaluated at import, so every item in every
    # response shared a single key and clients collapsed their user data.
    key: str = ""


class MediaStream(JellyModel):
    codec: str = "flac"
    type: str = "Audio"
    index: int = 0
    is_default: bool = True
    is_forced: bool = False
    is_external: bool = False
    is_interlaced: bool = False
    is_avc: bool = False
    is_hearing_impaired: bool = False
    is_text_subtitle_stream: bool = False
    supports_external_stream: bool = False
    channel_layout: str = "stereo"
    bit_rate: int = 320000
    channels: int = 2
    sample_rate: int = 44100
    bit_depth: int = 16


class MediaSource(JellyModel):
    id: str = ""
    name: str = "Track"
    protocol: str = "Http"
    path: str = ""
    type: str = "Default"
    # Tidal delivers FLAC-in-fMP4 or AAC-in-MP4; never MPEG-TS, which is what
    # this used to claim. Left as None for remote sources - we'd need a HEAD
    # against the CDN per request to know it, and a wrong size is worse than
    # none. A local copy can fill it in from the file on disk.
    container: str = "mp4"
    size: int | None = None
    is_remote: bool = False
    etag: str = ""
    run_time_ticks: int = 0
    read_at_native_framerate: bool = False
    ignore_dts: bool = False
    ignore_index: bool = False
    gen_pts_input: bool = False
    supports_transcoding: bool = True
    # Deliberately false for Tidal sources. Advertising direct stream invites
    # clients to pull bytes from /Items/<id>/File, which resolves to the
    # single-file (lossy) rendition - that would silently downgrade playback
    # from 24-bit FLAC to AAC. Only a local copy should set these true.
    supports_direct_stream: bool = False
    supports_direct_play: bool = False
    is_infinite_stream: bool = False
    requires_opening: bool = False
    requires_closing: bool = False
    requires_looping: bool = False
    supports_probing: bool = True
    media_streams: list[MediaStream] = []
    formats: list[str] = []
    bitrate: int = 320000
    required_http_headers: dict = {}
    transcoding_url: str | None = None
    transcoding_sub_protocol: str = "hls"
    # The HLS playlist points at Tidal's fMP4 segments, not TS ones.
    transcoding_container: str = "mp4"

    @classmethod
    def hls_for(cls, item_id: str, run_time_ticks: int = 0,
                api_key: str | None = None, kind: str = "hls") -> "MediaSource":
        """Build a MediaSource that points clients at the right playback URL.

        The token goes in the URL because that is what Jellyfin does and what
        clients need: this URL is handed straight to a media player, which will
        not be attaching authentication headers of its own.

        [kind] is "hls" unless the caller already knows (from a live call to
        Tidal, e.g. in /PlaybackInfo) that this track has no segmented
        rendition, in which case it is "direct" and the client should be
        pointed at the plain redirect endpoint instead of an HLS playlist.
        Defaults to "hls" for callers that only need a placeholder MediaSource
        (item listings) and are not about to pay for a live Tidal round trip
        just to fill it in.
        """
        query = f"MediaSourceId={item_id}"
        if api_key:
            query += f"&api_key={api_key}"
        path = (
            f"/Audio/{item_id}/stream.mp4"
            if kind == "direct"
            else f"/Audio/{item_id}/master.m3u8"
        )
        return cls(
            id=item_id,
            run_time_ticks=run_time_ticks,
            transcoding_url=f"{path}?{query}",
            transcoding_sub_protocol="http" if kind == "direct" else "hls",
        )


class MinimalArtistElements(JellyModel):
    id: str
    name: str


class Artist(JellyModel):
    id: str
    name: str
    server_id: str
    type: str = "MusicArtist"
    is_folder: bool = True
    user_data: UserData = UserData()
    image_tags: dict[str, str] = {}
    backdrop_image_tags: list[str] = []


class Album(JellyModel):
    id: str
    name: str
    server_id: str
    album_artist: str
    artists: list[str]
    album_artists: list[MinimalArtistElements]
    artist_items: list[MinimalArtistElements]
    child_count: int
    production_year: int | None = None
    premiere_date: str
    is_folder: bool = True
    type: str = "MusicAlbum"
    date_created: str = "2024-01-01T00:00:00.0000000Z"
    genres: list[str] = []
    genre_items: list[str] = []
    user_data: UserData = UserData()
    image_tags: dict[str, str] = {}
    backdrop_image_tags: list[str] = []

    @classmethod
    def create(cls, id, name, server_id, album_artist, artist_mapping, child_count, production_year, premiere_date, cover_id=None, user_data=None):
        return Album(
            id=id,
            name=name,
            server_id=server_id,
            album_artist=album_artist,
            artists=[artist.name for artist in artist_mapping],
            album_artists=artist_mapping,
            artist_items=artist_mapping,
            child_count=child_count,
            production_year=production_year,
            premiere_date=premiere_date,
            image_tags={"Primary": cover_id} if cover_id else {},
            user_data=user_data or UserData(),
        )


class Track(JellyModel):
    id: str
    name: str
    server_id: str
    album: str
    album_id: str
    album_artist: str
    artists: list[str]
    album_artists: list[MinimalArtistElements]
    artist_items: list[MinimalArtistElements]
    production_year: int | None = None
    run_time_ticks: int
    media_sources: list[MediaSource]
    user_data: UserData = UserData()
    genres: list[str] = []
    genre_items: list[str] = []
    date_created: str = "2024-01-01T00:00:00.0000000Z"
    container: str = "mp4"          # see MediaSource.container
    type: str = "Audio"
    media_type: str = "Audio"
    is_folder: bool = False
    can_delete: bool = False
    can_download: bool = False
    has_lyrics: bool = False
    has_subtitles: bool = False
    location_type: str = "Remote"
    index_number: int = 1
    parent_index_number: int = 1
    image_tags: dict[str, str] = {}
    backdrop_image_tags: list[str] = []
    parent_backdrop_image_tags: list[str] = []
    image_blur_hashes: dict = {}
    media_streams: list[MediaStream] = [MediaStream()]

    @classmethod
    def create(cls, id, name, server_id, album, album_id, album_artist, artist_mapping, production_year, run_time_ticks, cover_id=None, index_number=1, parent_index_number=1, user_data=None):
        return Track(
            id=id,
            name=name,
            server_id=server_id,
            album=album,
            album_id=album_id,
            album_artist=album_artist,
            artists=[artist.name for artist in artist_mapping],
            album_artists=artist_mapping,
            artist_items=artist_mapping,
            production_year=production_year,
            run_time_ticks=run_time_ticks,
            media_sources=[MediaSource.hls_for(id, run_time_ticks=run_time_ticks)],
            image_tags={"Primary": cover_id} if cover_id else {},
            index_number=index_number,
            parent_index_number=parent_index_number,
            user_data=user_data or UserData(),
        )


class Playlist(JellyModel):
    id: str
    name: str
    server_id: str
    type: str = "Playlist"
    media_type: str = "Audio"
    is_folder: bool = True
    child_count: int = 0
    overview: str = ""
    run_time_ticks: int = 0
    user_data: UserData = UserData()
    image_tags: dict[str, str] = {}
    backdrop_image_tags: list[str] = []

    @classmethod
    def create(cls, id, name, server_id, child_count=0, overview="", run_time_ticks=0, cover_id=None, user_data=None):
        return Playlist(
            id=id,
            name=name,
            server_id=server_id,
            child_count=child_count,
            overview=overview,
            run_time_ticks=run_time_ticks,
            image_tags={"Primary": cover_id} if cover_id else {},
            user_data=user_data or UserData(),
        )


class ResultWrapper(JellyModel):
    total_record_count: int
    start_index: int
    items: list

