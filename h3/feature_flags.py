import os


# 活动重新开放时改为 True；关闭时不会请求或展示任何秒杀信息。
SECKILL_ENABLED = False

# 投票活动已结束；保留旧实现便于回滚，但运行时不再调用投票接口。
VOTE_ENABLED = False

# 上市礼包仅在 listing_gift.py 指定日期内执行。
LISTING_GIFT_ENABLED = True

# 第 1-5 组工作流会通过环境变量显式开启会员资料采集。
ACCOUNT_DATA_ENABLED = os.getenv("ACCOUNT_DATA_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "y", "on"
}
