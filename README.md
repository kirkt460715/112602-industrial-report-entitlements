# 工业报表服务的用途授权与换档履约

资料源、用途、授权部位和报告交付各自登记。分配额度用演练单位表示。报表产出方法不属于对端协议。

所有资料供本地联调，为合成记录。`samples/register.json` 是基础台账，`samples/intake.json` 是送达的业务事实，`samples/receipts.json` 是对端查询合同。

## 本地资料端

使用 Python 3.11.2 标准库。服务只监听回环地址。

```sh
python3 -m unittest -v
python3 partner_feed.py --port 8765
```

- `GET /register` 读取台账。
- `GET /intake?since=0&limit=50` 读取一页到达记录，将返回的 `cursor` 传给下一次查询的 `since`。消息含 `sequence`、`at`、`source`、`facts`，其中 `at` 是收到记录的时间，业务发生时间可在 `facts` 中明确报告。
- `GET /receipt?action=操作&desk=经办方&ref=编号` 以三项值匹配样例。对端返回 `evidence` 作为独立事实。标记 `delayed` 的样例首次查询返回 503，后续可以读取回执。未知编号返回 404。联调端不会自行办理业务操作。

通过 `--samples` 指定另一组同名资料可换样例。临时不可查的状态按联调进程保存。

## 园区经办后端

`backend.py` 在资料端之上办理业务：消费到达记录，维护订单、档位、许可、额度与交付的一致台账。报表内容取自台账合成产物，不做分析模型。

```sh
python3 partner_feed.py --port 8765 &
python3 backend.py --port 8766 --feed http://127.0.0.1:8765
```

办理规则：

- 接单区分工厂自读与受托查阅，逐来源核对用途、粒度、厂区与有效期；供应方回执确认的权利优先于台账登记。
- 档位变更只调整额度与并行度，不放宽任何资料权利；已受理请求的占用延续到新档位，同一实际交付只记一次。
- 供应方缩窄授权后，已发出的包保留实发对象、版本与时间用于既有审计；新出包按新权利办理。
- 未出包的订单可受理合规缩减；范围不足时须用户确认后才受理。
- 未交付请求撤销即释放占用；回执未核实的交付继续占用；受托方退出后，旧查询不能再取包。

接口（只监听回环地址）：

- `POST /sync` 拉取到达记录并核对未决回执。
- `GET /factory/{工厂}/account` 可继续领取的报告、未兑现的额度和收费依据。
- `GET /supplier/{来源}/usage` 授权被用于哪些交付，以及当前许可状态。
- `GET /operator/orders/{编号}` 订单明细与当前合规评估；`GET /operator/overview` 经办总览。
- `POST /operator/orders/{编号}/reduce` 受理缩减（`confirm_insufficient` 确认范围不足）。
- `POST /operator/orders/{编号}/cancel` 撤销未交付请求并释放额度。
- `GET /deliveries/{编号}` 实发内容、版本、对象与回执状态。
- `GET /delegate/{受托方}/packages/{编号}` 受托方取包；退出后返回 403。
