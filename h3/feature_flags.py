import os


# 活动重新开放时改为 True；关闭时不会请求或展示任何秒杀信息。
SECKILL_ENABLED = False

# 活动日期仍由 campaign_vote.py 严格限制；非活动日不会请求投票接口。
VOTE_ENABLED = True

# 每月礼包仅在每月 30 日、且仅 new 组执行。
LISTING_GIFT_ENABLED = True

# 动态组工作流会通过环境变量显式开启会员资料采集。
ACCOUNT_DATA_ENABLED = os.getenv("ACCOUNT_DATA_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "y", "on"
}
