from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class SiteKind(StrEnum):
    MTEAM = "MTEAM"
    HDTIME = "HDTIME"
    HHCLUB = "HHCLUB"
    KEEPFRDS = "KEEPFRDS"
    HDHOME = "HDHOME"
    UBITS = "UBITS"
    HDFANS = "HDFANS"
    BTSCHOOL = "BTSCHOOL"
    PTTIME = "PTTIME"
    ROUSI_PRO = "ROUSI_PRO"
    LINGYIN_CLUB = "LINGYIN_CLUB"
    PTERCLUB = "PTERCLUB"
    AUDIENCES = "AUDIENCES"
    SPRING_SUNDAY = "SPRING_SUNDAY"
    HDDOLBY = "HDDOLBY"
    U2 = "U2"
    TANGPT = "TANGPT"
    CARPT = "CARPT"
    N13CITY = "13CITY"
    N1PTBA = "1PTBA"
    N52MOVIE = "52MOVIE"
    N52PT = "52PT"
    AGSVPT = "AGSVPT"
    ALINGPT = "ALINGPT"
    BAOZI = "BAOZI"
    BEITAI = "BEITAI"
    BYRBT = "BYRBT"
    CBG = "CBG"
    CDY = "CDY"
    CHDBITS = "CHDBITS"
    CNSCG = "CNSCG"
    CSPT = "CSPT"
    CYANBUG = "CYANBUG"
    DABAOJIAN = "DABAOJIAN"
    DAJIAO = "DAJIAO"
    DHTCLUB = "DHTCLUB"
    DISCFAN = "DISCFAN"
    DRAGONHD = "DRAGONHD"
    DSTUDIO = "DSTUDIO"
    DUBHE = "DUBHE"
    ECUSTPT = "ECUSTPT"
    FHPT = "FHPT"
    FILEPT = "FILEPT"
    FREEFARM = "FREEFARM"
    GAINBOUND = "GAINBOUND"
    HAIDAN = "HAIDAN"
    HARES = "HARES"
    HD4FANS = "HD4FANS"
    HDAI = "HDAI"
    HDAREA = "HDAREA"
    HDATMOS = "HDATMOS"
    HDCITY = "HDCITY"
    HDDISK = "HDDISK"
    HDKYLIN = "HDKYLIN"
    HDPT = "HDPT"
    HDSKY = "HDSKY"
    HDSTREET = "HDSTREET"
    HDUPT = "HDUPT"
    HDVIDEO = "HDVIDEO"
    HDZONE = "HDZONE"
    HITPT = "HITPT"
    HSPT = "HSPT"
    HUDBT = "HUDBT"
    ICC2022 = "ICC2022"
    ITZMX = "ITZMX"
    JOYHD = "JOYHD"
    KUFEI = "KUFEI"
    KUNLUN = "KUNLUN"
    LAJIDUI = "LAJIDUI"
    LEMONHD = "LEMONHD"
    LEMONHDNET = "LEMONHDNET"
    LEMONHDORG = "LEMONHDORG"
    MARCH = "MARCH"
    MOECAT = "MOECAT"
    MUXUEGE = "MUXUEGE"
    MYPT = "MYPT"
    NANYANGPT = "NANYANGPT"
    NOVAHD = "NOVAHD"
    NPUPT = "NPUPT"
    NWSUAF6 = "NWSUAF6"
    OURBITS = "OURBITS"
    PT99 = "PT99"
    PTCHINA = "PTCHINA"
    PTDREAM = "PTDREAM"
    PTGTK = "PTGTK"
    PTHOME = "PTHOME"
    PTLGS = "PTLGS"
    PTMSG = "PTMSG"
    PTSBAO = "PTSBAO"
    PTVICOMO = "PTVICOMO"
    PTZONE = "PTZONE"
    QINGYINGPT = "QINGYINGPT"
    RAILGUNPT = "RAILGUNPT"
    RAINGFH = "RAINGFH"
    RETROFLIX = "RETROFLIX"
    RS = "RS"
    SBPT = "SBPT"
    SEWERPT = "SEWERPT"
    SHARKPT = "SHARKPT"
    SJTU = "SJTU"
    STSPT = "STSPT"
    TCCF = "TCCF"
    TEY = "TEY"
    TJUPT = "TJUPT"
    TLFBITS = "TLFBITS"
    TMPT = "TMPT"
    TOKYOPT = "TOKYOPT"
    TOSKY = "TOSKY"
    ULTRAHD = "ULTRAHD"
    WHUPT = "WHUPT"
    WINTERSAKURA = "WINTERSAKURA"
    XDYPT = "XDYPT"
    XINGWAN = "XINGWAN"
    XINGYUNGE = "XINGYUNGE"
    YINGK = "YINGK"
    YINGUSKG = "YINGUSKG"
    ZRPT = "ZRPT"


class SiteCredentialKind(StrEnum):
    API_KEY = "API_KEY"
    COOKIE = "COOKIE"


class SiteProbeStatus(StrEnum):
    UNTESTED = "UNTESTED"
    OK = "OK"
    FAILED = "FAILED"


class SiteSupportStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PENDING_ADAPTER = "PENDING_ADAPTER"
    PENDING_REAL_VALIDATION = "PENDING_REAL_VALIDATION"


@dataclass(frozen=True, slots=True)
class SiteProfile:
    kind: SiteKind
    display_name: str
    base_url: str
    credential_kind: SiteCredentialKind
    request_timeout_seconds: int = 15
    search_interval_seconds: float = 0.0
    supports_user_agent: bool = False
    supports_browser_emulation: bool = False
    supports_proxy: bool = True
    support_status: SiteSupportStatus = SiteSupportStatus.SUPPORTED

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("站点 Profile 展示名称不能为空")
        if not 1 <= self.request_timeout_seconds <= 120:
            raise ValueError("站点 Profile 请求超时必须在 1～120 秒之间")
        if not 0 <= self.search_interval_seconds <= 3600:
            raise ValueError("站点 Profile 搜索间隔必须在 0～3600 秒之间")


SITE_PROFILE_REGISTRY: dict[SiteKind, SiteProfile] = {
    SiteKind.MTEAM: SiteProfile(
        kind=SiteKind.MTEAM,
        display_name="M-TEAM",
        base_url="https://kp.m-team.cc",
        credential_kind=SiteCredentialKind.API_KEY,
    ),
    SiteKind.HDTIME: SiteProfile(
        kind=SiteKind.HDTIME,
        display_name="HDTime",
        base_url="https://hdtime.org",
        credential_kind=SiteCredentialKind.COOKIE,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.HHCLUB: SiteProfile(
        kind=SiteKind.HHCLUB,
        display_name="HHClub",
        base_url="https://hhanclub.net",
        credential_kind=SiteCredentialKind.COOKIE,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.KEEPFRDS: SiteProfile(
        kind=SiteKind.KEEPFRDS,
        display_name="KeepFrds",
        base_url="https://pt.keepfrds.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.HDHOME: SiteProfile(
        kind=SiteKind.HDHOME,
        display_name="HDHome",
        base_url="https://hdhome.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.UBITS: SiteProfile(
        kind=SiteKind.UBITS,
        display_name="UBits",
        base_url="https://ubits.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.HDFANS: SiteProfile(
        kind=SiteKind.HDFANS,
        display_name="HDFans",
        base_url="https://hdfans.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.BTSCHOOL: SiteProfile(
        kind=SiteKind.BTSCHOOL,
        display_name="BTSCHOOL",
        base_url="https://pt.btschool.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.PTTIME: SiteProfile(
        kind=SiteKind.PTTIME,
        display_name="PTTime",
        base_url="https://www.pttime.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.ROUSI_PRO: SiteProfile(
        kind=SiteKind.ROUSI_PRO,
        display_name="Rousi Pro",
        base_url="https://rousi.pro",
        credential_kind=SiteCredentialKind.API_KEY,
    ),
    SiteKind.LINGYIN_CLUB: SiteProfile(
        kind=SiteKind.LINGYIN_CLUB,
        display_name="聆音Club",
        base_url="https://pt.soulvoice.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.PTERCLUB: SiteProfile(
        kind=SiteKind.PTERCLUB,
        display_name="PTerClub",
        base_url="https://pterclub.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.AUDIENCES: SiteProfile(
        kind=SiteKind.AUDIENCES,
        display_name="Audiences",
        base_url="https://audiences.me",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.SPRING_SUNDAY: SiteProfile(
        kind=SiteKind.SPRING_SUNDAY,
        display_name="SpringSunday",
        base_url="https://springsunday.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.HDDOLBY: SiteProfile(
        kind=SiteKind.HDDOLBY,
        display_name="HDDolby",
        base_url="https://www.uhddolby.com",
        credential_kind=SiteCredentialKind.COOKIE,
        # Confirmed with one real read-only search on 2026-10-11:
        # HDDolby succeeds at a 45-second budget but can exceed 15 seconds.
        request_timeout_seconds=45,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.U2: SiteProfile(
        kind=SiteKind.U2,
        display_name="U2",
        base_url="https://u2.dmhy.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.TANGPT: SiteProfile(
        kind=SiteKind.TANGPT,
        display_name="不可躺",
        base_url="https://www.tangpt.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.CARPT: SiteProfile(
        kind=SiteKind.CARPT,
        display_name="CarPT",
        base_url="https://carpt.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
    ),
    SiteKind.N13CITY: SiteProfile(
        kind=SiteKind.N13CITY,
        display_name="13city",
        base_url="https://13city.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.N1PTBA: SiteProfile(
        kind=SiteKind.N1PTBA,
        display_name="1PTBar",
        base_url="https://1ptba.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.N52MOVIE: SiteProfile(
        kind=SiteKind.N52MOVIE,
        display_name="52movie",
        base_url="https://www.52movie.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.N52PT: SiteProfile(
        kind=SiteKind.N52PT,
        display_name="52PT",
        base_url="https://52pt.site",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.AGSVPT: SiteProfile(
        kind=SiteKind.AGSVPT,
        display_name="AGSVPT",
        base_url="https://pt.agsvpt.cn",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.ALINGPT: SiteProfile(
        kind=SiteKind.ALINGPT,
        display_name="alingPT",
        base_url="https://pt.aling.de",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.BAOZI: SiteProfile(
        kind=SiteKind.BAOZI,
        display_name="包子",
        base_url="https://p.t-baozi.cc",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.BEITAI: SiteProfile(
        kind=SiteKind.BEITAI,
        display_name="备胎",
        base_url="https://www.beitai.pt",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.BYRBT: SiteProfile(
        kind=SiteKind.BYRBT,
        display_name="北邮人-BYRBT",
        base_url="https://byr.pt",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.CBG: SiteProfile(
        kind=SiteKind.CBG,
        display_name="藏宝阁",
        base_url="https://cangbao.ge",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.CDY: SiteProfile(
        kind=SiteKind.CDY,
        display_name="传道院",
        base_url="https://pt.cdy.pics",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.CHDBITS: SiteProfile(
        kind=SiteKind.CHDBITS,
        display_name="CHDBits",
        base_url="https://ptchdbits.co",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.CNSCG: SiteProfile(
        kind=SiteKind.CNSCG,
        display_name="圣城家园PT",
        base_url="https://www.cnscg.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.CSPT: SiteProfile(
        kind=SiteKind.CSPT,
        display_name="财神",
        base_url="https://cspt.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.CYANBUG: SiteProfile(
        kind=SiteKind.CYANBUG,
        display_name="CyanBug",
        base_url="https://cyanbug.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.DABAOJIAN: SiteProfile(
        kind=SiteKind.DABAOJIAN,
        display_name="大宝剑",
        base_url="https://pt.525968.xyz",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.DAJIAO: SiteProfile(
        kind=SiteKind.DAJIAO,
        display_name="DAJIAO",
        base_url="https://dajiao.cyou",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.DHTCLUB: SiteProfile(
        kind=SiteKind.DHTCLUB,
        display_name="DHTCLUB PT",
        base_url="https://pt.dhtclub.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.DISCFAN: SiteProfile(
        kind=SiteKind.DISCFAN,
        display_name="DiscFan",
        base_url="https://discfan.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.DRAGONHD: SiteProfile(
        kind=SiteKind.DRAGONHD,
        display_name="龍之家",
        base_url="https://www.dragonhd.xyz",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.DSTUDIO: SiteProfile(
        kind=SiteKind.DSTUDIO,
        display_name="Depth Studio",
        base_url="https://dstudio.me",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.DUBHE: SiteProfile(
        kind=SiteKind.DUBHE,
        display_name="天枢",
        base_url="https://dubhe.site",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.ECUSTPT: SiteProfile(
        kind=SiteKind.ECUSTPT,
        display_name="ECUSTPT",
        base_url="https://pt.ecust.pp.ua",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.FHPT: SiteProfile(
        kind=SiteKind.FHPT,
        display_name="凤凰PT",
        base_url="https://pt.521.best",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.FILEPT: SiteProfile(
        kind=SiteKind.FILEPT,
        display_name="Filept",
        base_url="https://www.filept.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.FREEFARM: SiteProfile(
        kind=SiteKind.FREEFARM,
        display_name="自由农场",
        base_url="https://pt.0ff.cc",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.GAINBOUND: SiteProfile(
        kind=SiteKind.GAINBOUND,
        display_name="丐帮",
        base_url="https://gainbound.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HAIDAN: SiteProfile(
        kind=SiteKind.HAIDAN,
        display_name="海胆",
        base_url="https://www.haidan.cc",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HARES: SiteProfile(
        kind=SiteKind.HARES,
        display_name="HaresClub",
        base_url="https://club.hares.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HD4FANS: SiteProfile(
        kind=SiteKind.HD4FANS,
        display_name="HD4FANS",
        base_url="https://pt.hd4fans.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDAI: SiteProfile(
        kind=SiteKind.HDAI,
        display_name="HDAI",
        base_url="https://www.hd.ai",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDAREA: SiteProfile(
        kind=SiteKind.HDAREA,
        display_name="HDArea",
        base_url="https://hdarea.club",
        credential_kind=SiteCredentialKind.API_KEY,
        search_interval_seconds=2.0,
        supports_user_agent=False,
        supports_browser_emulation=False,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDATMOS: SiteProfile(
        kind=SiteKind.HDATMOS,
        display_name="HDATMOS",
        base_url="https://hdatmos.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDCITY: SiteProfile(
        kind=SiteKind.HDCITY,
        display_name="HDCity",
        base_url="https://hdcity.city",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDDISK: SiteProfile(
        kind=SiteKind.HDDISK,
        display_name="HDDisk",
        base_url="https://hddisk.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDKYLIN: SiteProfile(
        kind=SiteKind.HDKYLIN,
        display_name="HDKylin",
        base_url="https://www.hdkyl.in",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDPT: SiteProfile(
        kind=SiteKind.HDPT,
        display_name="明教",
        base_url="https://hdpt.xyz",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDSKY: SiteProfile(
        kind=SiteKind.HDSKY,
        display_name="HDSky",
        base_url="https://hdsky.me",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDSTREET: SiteProfile(
        kind=SiteKind.HDSTREET,
        display_name="高清街",
        base_url="https://hdstreet.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDUPT: SiteProfile(
        kind=SiteKind.HDUPT,
        display_name="HDU",
        base_url="https://pt.hdupt.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDVIDEO: SiteProfile(
        kind=SiteKind.HDVIDEO,
        display_name="HDVideo",
        base_url="https://hdvideo.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HDZONE: SiteProfile(
        kind=SiteKind.HDZONE,
        display_name="HDZone",
        base_url="https://hdfun.me",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HITPT: SiteProfile(
        kind=SiteKind.HITPT,
        display_name="百川PT",
        base_url="https://www.hitpt.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HSPT: SiteProfile(
        kind=SiteKind.HSPT,
        display_name="回声PT",
        base_url="https://hspt.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.HUDBT: SiteProfile(
        kind=SiteKind.HUDBT,
        display_name="蝴蝶-HUDBT",
        base_url="https://hudbt.hust.edu.cn",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.ICC2022: SiteProfile(
        kind=SiteKind.ICC2022,
        display_name="ICC",
        base_url="https://www.icc2022.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.ITZMX: SiteProfile(
        kind=SiteKind.ITZMX,
        display_name="PT分享站",
        base_url="https://pt.itzmx.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.JOYHD: SiteProfile(
        kind=SiteKind.JOYHD,
        display_name="JoyHD",
        base_url="https://www.joyhd.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.KUFEI: SiteProfile(
        kind=SiteKind.KUFEI,
        display_name="库非",
        base_url="https://kufei.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.KUNLUN: SiteProfile(
        kind=SiteKind.KUNLUN,
        display_name="昆仑",
        base_url="https://www.yhpp.cc",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.LAJIDUI: SiteProfile(
        kind=SiteKind.LAJIDUI,
        display_name="lajidui",
        base_url="https://pt.lajidui.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.LEMONHD: SiteProfile(
        kind=SiteKind.LEMONHD,
        # 与 LEMONHDORG 同名会撞站点名称唯一约束；上游 aka 为「柠檬不酸」，
        # 且 lemonhd.club 自 2026-03-13 起已标记 isDead，故改用中文名区分。
        display_name="柠檬不酸",
        base_url="https://lemonhd.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.LEMONHDNET: SiteProfile(
        kind=SiteKind.LEMONHDNET,
        display_name="柠檬不甜",
        base_url="https://lemonhd.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.LEMONHDORG: SiteProfile(
        kind=SiteKind.LEMONHDORG,
        display_name="LemonHD",
        base_url="https://lemonhd.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.MARCH: SiteProfile(
        kind=SiteKind.MARCH,
        display_name="March-Media",
        base_url="https://duckboobee.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.MOECAT: SiteProfile(
        kind=SiteKind.MOECAT,
        display_name="MoeCat",
        base_url="https://moecat.best",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.MUXUEGE: SiteProfile(
        kind=SiteKind.MUXUEGE,
        display_name="慕雪阁",
        base_url="https://pt.muxuege.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.MYPT: SiteProfile(
        kind=SiteKind.MYPT,
        display_name="MyPT",
        base_url="https://cc.mypt.cc",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.NANYANGPT: SiteProfile(
        kind=SiteKind.NANYANGPT,
        display_name="南洋PT",
        base_url="https://nanyangpt.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.NOVAHD: SiteProfile(
        kind=SiteKind.NOVAHD,
        display_name="NovaHD",
        base_url="https://pt.novahd.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.NPUPT: SiteProfile(
        kind=SiteKind.NPUPT,
        display_name="NPUBits",
        base_url="https://npupt.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.NWSUAF6: SiteProfile(
        kind=SiteKind.NWSUAF6,
        display_name="麦田PT",
        base_url="https://pt.nwsuaf6.edu.cn",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.OURBITS: SiteProfile(
        kind=SiteKind.OURBITS,
        display_name="OurBits",
        base_url="https://ourbits.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        # v1.1.18：账号持有者发起真实只读验收，先开放正式任务入口；
        # 验收未闭环前不得写入任何真实 E2E 结论。
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.PT99: SiteProfile(
        kind=SiteKind.PT99,
        display_name="PT99",
        base_url="https://pt.j99.info",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.PTCHINA: SiteProfile(
        kind=SiteKind.PTCHINA,
        display_name="PTChina",
        base_url="https://ptchina.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.PTDREAM: SiteProfile(
        kind=SiteKind.PTDREAM,
        display_name="PTDream",
        base_url="https://www.ptdream.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.PTGTK: SiteProfile(
        kind=SiteKind.PTGTK,
        display_name="PT GTK",
        base_url="https://pt.gtkpw.xyz",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.PTHOME: SiteProfile(
        kind=SiteKind.PTHOME,
        display_name="PTHome",
        base_url="https://pthome.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.PTLGS: SiteProfile(
        kind=SiteKind.PTLGS,
        display_name="PTLGS",
        base_url="https://ptlgs.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.PTMSG: SiteProfile(
        kind=SiteKind.PTMSG,
        display_name="PTMSG",
        base_url="https://pt.msg.vg",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.PTSBAO: SiteProfile(
        kind=SiteKind.PTSBAO,
        display_name="PTSBao",
        base_url="https://ptsbao.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.PTVICOMO: SiteProfile(
        kind=SiteKind.PTVICOMO,
        display_name="象岛",
        base_url="https://ptvicomo.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.PTZONE: SiteProfile(
        kind=SiteKind.PTZONE,
        display_name="PTzone",
        base_url="https://ptzone.xyz",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.QINGYINGPT: SiteProfile(
        kind=SiteKind.QINGYINGPT,
        display_name="清影PT",
        base_url="https://hitpt.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.RAILGUNPT: SiteProfile(
        kind=SiteKind.RAILGUNPT,
        display_name="RailgunPT",
        base_url="https://bilibili.download",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.RAINGFH: SiteProfile(
        kind=SiteKind.RAINGFH,
        display_name="雨",
        base_url="https://raingfh.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.RETROFLIX: SiteProfile(
        kind=SiteKind.RETROFLIX,
        display_name="RetroFlix",
        base_url="https://retroflix.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.RS: SiteProfile(
        kind=SiteKind.RS,
        display_name="睿思",
        base_url="https://resource.xidian.edu.cn",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.SBPT: SiteProfile(
        kind=SiteKind.SBPT,
        display_name="SBPT",
        base_url="https://sbpt.link",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.SEWERPT: SiteProfile(
        kind=SiteKind.SEWERPT,
        display_name="下水道",
        base_url="https://sewerpt.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.SHARKPT: SiteProfile(
        kind=SiteKind.SHARKPT,
        display_name="鲨鱼PT",
        base_url="https://sharkpt.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.SJTU: SiteProfile(
        kind=SiteKind.SJTU,
        display_name="葡萄",
        base_url="https://pt.sjtu.edu.cn",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.STSPT: SiteProfile(
        kind=SiteKind.STSPT,
        display_name="STSPT",
        base_url="https://www.sts.tw",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.TCCF: SiteProfile(
        kind=SiteKind.TCCF,
        display_name="TorrentCCF",
        base_url="https://et8.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.TEY: SiteProfile(
        kind=SiteKind.TEY,
        display_name="tey",
        base_url="https://pt.tey.cc",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.TJUPT: SiteProfile(
        kind=SiteKind.TJUPT,
        display_name="北洋园PT",
        base_url="https://tjupt.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.TLFBITS: SiteProfile(
        kind=SiteKind.TLFBITS,
        display_name="TLFBits",
        base_url="https://pt.eastgame.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.TMPT: SiteProfile(
        kind=SiteKind.TMPT,
        display_name="唐门",
        base_url="https://tmpt.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.TOKYOPT: SiteProfile(
        kind=SiteKind.TOKYOPT,
        display_name="Tokyo",
        base_url="https://www.tokyo-manga.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.TOSKY: SiteProfile(
        kind=SiteKind.TOSKY,
        display_name="ToSky",
        base_url="https://t.tosky.club",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.ULTRAHD: SiteProfile(
        kind=SiteKind.ULTRAHD,
        display_name="UltraHD",
        base_url="https://ultrahd.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.WHUPT: SiteProfile(
        kind=SiteKind.WHUPT,
        display_name="珞樱-WHUPT",
        base_url="https://whu.pt",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.WINTERSAKURA: SiteProfile(
        kind=SiteKind.WINTERSAKURA,
        display_name="WinterSakura",
        base_url="https://wintersakura.net",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.XDYPT: SiteProfile(
        kind=SiteKind.XDYPT,
        display_name="修道院",
        base_url="https://xdypt.vip",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.XINGWAN: SiteProfile(
        kind=SiteKind.XINGWAN,
        display_name="星湾",
        base_url="https://xingwan.cc",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.XINGYUNGE: SiteProfile(
        kind=SiteKind.XINGYUNGE,
        display_name="星陨阁",
        base_url="https://pt.xingyungept.org",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.YINGK: SiteProfile(
        kind=SiteKind.YINGK,
        display_name="影客",
        base_url="https://yingk.com",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.YINGUSKG: SiteProfile(
        kind=SiteKind.YINGUSKG,
        display_name="樱花",
        base_url="https://pt.ying.us.kg",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
    SiteKind.ZRPT: SiteProfile(
        kind=SiteKind.ZRPT,
        display_name="ZRPT",
        base_url="https://naturept.top",
        credential_kind=SiteCredentialKind.COOKIE,
        search_interval_seconds=2.0,
        supports_user_agent=True,
        supports_browser_emulation=True,
        support_status=SiteSupportStatus.SUPPORTED,
    ),
}


# Explicit allowlist of implemented adapters eligible for normal tasks.
# v1.1.18：按用户要求取消新增站点的全部准入限制，PT-depiler 来源的 99 个站点
# 一并纳入本表，可创建、可启用、可进入分析搜索与拆包任务。真实只读探测与完整
# 拆包/辅种验收结论仍单独记录在 docs/support-matrix.md，不在本表体现。
# Future site kinds are never implicitly admitted.
PERSISTED_SITE_KINDS = frozenset(
    {
        SiteKind.MTEAM,
        SiteKind.HDTIME,
        SiteKind.HHCLUB,
        SiteKind.KEEPFRDS,
        SiteKind.HDHOME,
        SiteKind.UBITS,
        SiteKind.HDFANS,
        SiteKind.BTSCHOOL,
        SiteKind.PTTIME,
        SiteKind.ROUSI_PRO,
        SiteKind.LINGYIN_CLUB,
        SiteKind.PTERCLUB,
        SiteKind.AUDIENCES,
        SiteKind.SPRING_SUNDAY,
        SiteKind.HDDOLBY,
        SiteKind.U2,
        SiteKind.TANGPT,
        SiteKind.CARPT,
        SiteKind.N13CITY,
        SiteKind.N1PTBA,
        SiteKind.N52MOVIE,
        SiteKind.N52PT,
        SiteKind.AGSVPT,
        SiteKind.ALINGPT,
        SiteKind.BAOZI,
        SiteKind.BEITAI,
        SiteKind.BYRBT,
        SiteKind.CBG,
        SiteKind.CDY,
        SiteKind.CHDBITS,
        SiteKind.CNSCG,
        SiteKind.CSPT,
        SiteKind.CYANBUG,
        SiteKind.DABAOJIAN,
        SiteKind.DAJIAO,
        SiteKind.DHTCLUB,
        SiteKind.DISCFAN,
        SiteKind.DRAGONHD,
        SiteKind.DSTUDIO,
        SiteKind.DUBHE,
        SiteKind.ECUSTPT,
        SiteKind.FHPT,
        SiteKind.FILEPT,
        SiteKind.FREEFARM,
        SiteKind.GAINBOUND,
        SiteKind.HAIDAN,
        SiteKind.HARES,
        SiteKind.HD4FANS,
        SiteKind.HDAI,
        SiteKind.HDAREA,
        SiteKind.HDATMOS,
        SiteKind.HDCITY,
        SiteKind.HDDISK,
        SiteKind.HDKYLIN,
        SiteKind.HDPT,
        SiteKind.HDSKY,
        SiteKind.HDSTREET,
        SiteKind.HDUPT,
        SiteKind.HDVIDEO,
        SiteKind.HDZONE,
        SiteKind.HITPT,
        SiteKind.HSPT,
        SiteKind.HUDBT,
        SiteKind.ICC2022,
        SiteKind.ITZMX,
        SiteKind.JOYHD,
        SiteKind.KUFEI,
        SiteKind.KUNLUN,
        SiteKind.LAJIDUI,
        SiteKind.LEMONHD,
        SiteKind.LEMONHDNET,
        SiteKind.LEMONHDORG,
        SiteKind.MARCH,
        SiteKind.MOECAT,
        SiteKind.MUXUEGE,
        SiteKind.MYPT,
        SiteKind.NANYANGPT,
        SiteKind.NOVAHD,
        SiteKind.NPUPT,
        SiteKind.NWSUAF6,
        SiteKind.OURBITS,
        SiteKind.PT99,
        SiteKind.PTCHINA,
        SiteKind.PTDREAM,
        SiteKind.PTGTK,
        SiteKind.PTHOME,
        SiteKind.PTLGS,
        SiteKind.PTMSG,
        SiteKind.PTSBAO,
        SiteKind.PTVICOMO,
        SiteKind.PTZONE,
        SiteKind.QINGYINGPT,
        SiteKind.RAILGUNPT,
        SiteKind.RAINGFH,
        SiteKind.RETROFLIX,
        SiteKind.RS,
        SiteKind.SBPT,
        SiteKind.SEWERPT,
        SiteKind.SHARKPT,
        SiteKind.SJTU,
        SiteKind.STSPT,
        SiteKind.TCCF,
        SiteKind.TEY,
        SiteKind.TJUPT,
        SiteKind.TLFBITS,
        SiteKind.TMPT,
        SiteKind.TOKYOPT,
        SiteKind.TOSKY,
        SiteKind.ULTRAHD,
        SiteKind.WHUPT,
        SiteKind.WINTERSAKURA,
        SiteKind.XDYPT,
        SiteKind.XINGWAN,
        SiteKind.XINGYUNGE,
        SiteKind.YINGK,
        SiteKind.YINGUSKG,
        SiteKind.ZRPT,
    }
)
CONFIGURABLE_SITE_KINDS = frozenset(SiteKind)
# 所有已注册类型都可入库；是否可执行由 PERSISTED_SITE_KINDS 决定。
DATABASE_SITE_KINDS = frozenset(SiteKind)
PERSISTED_SITE_CREDENTIAL_KINDS = frozenset({SiteCredentialKind.API_KEY, SiteCredentialKind.COOKIE})


DEFAULT_COOKIE_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def site_profiles() -> tuple[SiteProfile, ...]:
    return tuple(SITE_PROFILE_REGISTRY[kind] for kind in SiteKind if kind in SITE_PROFILE_REGISTRY)


def site_profile(kind: SiteKind) -> SiteProfile:
    try:
        return SITE_PROFILE_REGISTRY[kind]
    except KeyError as exc:
        raise ValueError("站点类型尚未注册 Profile") from exc


def trusted_site_base_url(kind: SiteKind) -> str:
    return site_profile(kind).base_url


def site_kind_is_persistable(kind: SiteKind) -> bool:
    return (
        kind in PERSISTED_SITE_KINDS
        and site_profile(kind).support_status is SiteSupportStatus.SUPPORTED
    )


def site_kind_is_configurable(kind: SiteKind) -> bool:
    return kind in CONFIGURABLE_SITE_KINDS and site_profile(kind).support_status in {
        SiteSupportStatus.SUPPORTED,
        SiteSupportStatus.PENDING_REAL_VALIDATION,
    }


def normalize_site_base_url(kind: SiteKind, value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("站点 base URL 无效") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("站点 base URL 必须是无凭证、无路径的 HTTPS origin")
    host = parsed.hostname.casefold().rstrip(".")
    if not host or ":" in host:
        raise ValueError("站点 base URL host 无效")
    if kind is SiteKind.MTEAM and (
        port not in {None, 443} or not (host == "m-team.cc" or host.endswith(".m-team.cc"))
    ):
        raise ValueError("M-Team 站点地址必须位于 https://*.m-team.cc")
    netloc = host if port in {None, 443} else f"{host}:{port}"
    normalized = f"https://{netloc}"
    if kind is not SiteKind.MTEAM and normalized != trusted_site_base_url(kind):
        raise ValueError(f"{site_profile(kind).display_name} 站点地址必须是受信任 Profile 固定地址")
    return normalized


def required_site_credential_kind(kind: SiteKind) -> SiteCredentialKind:
    return site_profile(kind).credential_kind


def normalize_site_credential(kind: SiteCredentialKind, value: str) -> str:
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("站点凭证不能包含 NUL 或换行符")
    if kind is SiteCredentialKind.API_KEY:
        normalized = value.strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("API Key 不能为空且最长 512 个字符")
        return normalized
    if kind is SiteCredentialKind.COOKIE:
        if not value.strip() or len(value) > 8192:
            raise ValueError("Cookie 不能为空且最长 8192 个字符")
        return value
    raise ValueError("未知站点凭证类型")
