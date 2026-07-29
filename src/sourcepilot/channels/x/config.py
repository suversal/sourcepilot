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
#: 抓取自浏览器里的真实请求（2026-07-26）。原先那组是凭记忆写的，实测全部过期。
OPERATIONS: dict[str, str] = {
    "SearchTimeline": "kn0jeHGOUFYdNe_FUxwxsQ",
    "UserByScreenName": "2qvSHpkWTMS9i0zJAwDNiA",
    "UserTweets": "RIylB10EGWyBSs4ZXpQjCw",
    # 取单条推文。**长文（X Articles）的全文只有这条路能拿到**——搜索与时间线
    # 返回的 article 只有 preview_text，正文要靠下面那组 fieldToggles 打开。
    "TweetResultByRestId": "LkId5Akr61BS6BmOIcffRg",
}

#: 拉长文正文时必须打开的开关。默认全是关的，所以平时的搜索/时间线拿不到正文。
#: `withArticleRichContentState` 给结构化的 Draft.js（有标题层级和链接实体），
#: `withArticlePlainText` 给纯文本兜底——两个都要，前者解析失败时还有后者。
ARTICLE_FIELD_TOGGLES: dict[str, bool] = {
    "withArticleRichContentState": True,
    "withArticlePlainText": True,
    "withArticleSummaryText": True,
    "withArticleVoiceOver": False,
}

#: GraphQL 的 features flag。X 会不定期增删；缺字段时它会明确报
#: 「The following features cannot be null」，把报的字段补进来即可。
DEFAULT_FEATURES: dict[str, bool] = {
    "rweb_video_screen_enabled": False,
    "rweb_cashtags_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": True,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "rweb_conversational_replies_downvote_enabled": False,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

#: UserByScreenName 用的是另一套更短的 features——照抄真实请求，
#: 多给或少给都可能被拒（X 会明确报「The following features cannot be null」）。
USER_FEATURES: dict[str, bool] = {
    "hidden_profile_subscriptions_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": True,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
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

#: 这些 operation **强制要求 x-client-transaction-id**，且该签名是一次性的。
#:
#: 实测（2026-07-26，在真实登录态浏览器里对照验证）：
#:   UserByScreenName  不带签名 → 200 ✅
#:   UserTweets        不带签名 → 200 ✅（229KB 真实数据）
#:   UserMedia         不带签名 → 200 ✅
#:   SearchTimeline    不带签名 → 404 ❌
#:                     **带浏览器刚生成的签名重放 → 依然 404** ❌
#:
#: 最后那条最关键：签名不能截获复用，必须能**现场生成**（带时间戳/nonce）。
#: 所以搜索这条路绕不开复刻 twscrape 的 xclid 那套算法。
SIGNED_OPERATIONS = frozenset({"SearchTimeline"})

RATE_LIMIT_REMAINING_HEADER = "x-rate-limit-remaining"
RATE_LIMIT_RESET_HEADER = "x-rate-limit-reset"
