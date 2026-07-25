"""X 后端的**可变常量**，全部集中在这里。

CLAUDE.md 的铁律：「签名 / operation-id / 混淆常量单独抽文件——对方改版=改配置，
不改逻辑」。X 的 GraphQL operation id 会随前端发版轮换，features flag 会增删，
Nitter 公共实例更是每隔几个月换一批。这些都是**数据**，不该埋在代码里。

operation id 过期的表现是 GraphQL 返回 404。届时用 `discover.py` 从登录态页面
重新抓一份，或手动更新下面的常量。
"""

from __future__ import annotations

#: X web 端公开的 Bearer token。这不是秘密——它硬编码在 X 自己的前端里，
#: 匿名访客也拿得到，作用只是标识「我是 web 客户端」。
PUBLIC_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

GRAPHQL_BASE = "https://x.com/i/api/graphql"
GUEST_ACTIVATE = "https://api.x.com/1.1/guest/activate.json"

#: operation id → 名称。**会过期**，过期表现为 GraphQL 404。
#: 更新方式见模块文档。
OPERATIONS: dict[str, str] = {
    "SearchTimeline": "MJpyQGqgklrVl_0X9gNy3A",
    "UserByScreenName": "32pL5BWe9WKeSK1MoPvFQQ",
    "UserTweets": "V7H0Ap3_Hh2FyS75OCDO3Q",
    "TweetDetail": "VWFGPVAGkZMGRKGe3GFFnA",
}

#: GraphQL 的 features flag。X 会不定期增删；缺字段时它会明确报
#: 「The following features cannot be null」，把报的字段补进来即可。
DEFAULT_FEATURES: dict[str, bool] = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": False,
    "responsive_web_grok_share_attachment_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

#: FxTwitter：Discord 嵌入卡片用的公开服务，自己处理所有认证，零依赖零 Key。
FXTWITTER_BASE = "https://api.fxtwitter.com"

#: Nitter 公共实例。**寿命很短**——参考文档记着「公共实例 2026.03 起基本全挂」，
#: 实测 2026-07-26 只有 nitter.net 的时间线还活着，搜索全部关闭。
#: 自建实例填在源配置的 `nitter_instances` 里，会排在这些前面。
NITTER_INSTANCES: list[str] = [
    "https://nitter.net",
    "https://xcancel.com",
    "https://nitter.tiekoetter.com",
]

#: X 的错误码。参考 twscrape 的 `_check_rep`：这几个代表账号本身废了，
#: 不是临时限流——冷却再久也没用，得换账号。
FATAL_ERROR_CODES = frozenset({32, 64, 88, 89, 326})
#: 32=认证失败 64=账号被封 88=速率超限(账号级) 89=token 失效 326=账号被锁

RATE_LIMIT_REMAINING_HEADER = "x-rate-limit-remaining"
RATE_LIMIT_RESET_HEADER = "x-rate-limit-reset"
