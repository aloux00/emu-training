from sqlmodel import SQLModel, Field


class User(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str = ""
    name: str = ""
    role: str = "teacher"  # admin / jiaowu / teacher / student
    is_active: bool = True


class Teacher(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    emp_id: str = Field(unique=True, index=True)
    name: str = ""
    aliases: str = ""   # 别名（逗号分隔），如「王晓1,王晓2」：重名老师区分 / 文件里的惯用称呼
    can_teach: bool = True
    can_main_read: bool = True
    can_review: bool = True
    can_mentor: bool = True
    leave_dates: str = ""  # comma separated YYYY-MM-DD
    enabled: bool = True   # 启用状态：停用后不参与自动排课、不出现在人员选择名单（历史课表不受影响）
    phone: str = ""        # 钉钉绑定的手机号，用于自动创建待办提醒（可空）
    invite_token: str = ""  # 课程内容填报链接的令牌（钉钉待办 detailUrl 使用）


class Student(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    stu_id: str = Field(unique=True, index=True)
    name: str = ""
    grade: str = ""  # 一年级 / 二年级 / 三年级
    phone: str = ""  # 手机号：技能考核发布时用于钉钉单聊推送


class Graduate(SQLModel, table=True):
    """研究生名册：第三节「研究生汇报」的汇报人来源。"""
    id: int = Field(default=None, primary_key=True)
    grad_id: str = Field(unique=True, index=True)   # 学号/编号
    name: str = ""
    major: str = ""    # 专业/年级，可空
    phone: str = ""    # 钉钉绑定手机号，可空
    leave_dates: str = ""  # 请假日期，逗号分隔


class ClassType(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    name: str = ""
    color: str = "#185FA5"
    freq_desc: str = ""


class Menu(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    parent_id: int = 0
    name: str = ""
    path: str = ""
    icon: str = ""
    sort: int = 0
    permission: str = ""
    visible: bool = True


class ClassSession(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    year: int = 0
    month: int = 0
    date: str = ""  # YYYY-MM-DD，可因节假日手工调整
    slot: int = 1   # 1 or 2
    week_index: int = 0  # 第几周（生成时固定绑定，不随日期调整而变）
    class_type_id: int = 0
    class_type_name: str = ""
    teacher_id: int = 0
    main_reader_id: int = 0
    reviewer_id: int = 0
    mentor_id: int = 0  # 副主阅/指导老师（可选，高年资老师从旁指导）
    presenter_id: int = 0  # 汇报人（研究生汇报用，指向 Graduate.id）
    status: str = "draft"  # draft / published
    created_at: str = ""
    topic: str = ""  # 讲课内容/题目（教师通过钉钉待办链接填报）
    topic_by: int = 0    # 最后填写人（Teacher.id）；0 = 教务在网页端直接改的
    topic_at: str = ""   # 最后填写时间（YYYY-MM-DD HH:MM），用于"谁改了我的题目"追溯
    # 名字文本快照：生成/导入/改人时写入，名单里的人之后被删除或改名，
    # 历史课表仍按快照显示，不会变成空白。显示时优先快照、回退实时查询。
    teacher_name: str = ""      # 老师
    main_reader_name: str = ""  # 主阅（学生或小讲课老师）
    reviewer_name: str = ""     # 评审
    mentor_name: str = ""       # 指导
    presenter_name: str = ""    # 汇报人（研究生）


class Holiday(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    date: str = ""       # YYYY-MM-DD 停课日（如节假日）
    name: str = ""       # 名称，如「国庆节」
    makeup: str = ""     # 补讲/调课说明（可选）


class RuleConfig(SQLModel, table=True):
    """排课规则配置：key 固定为 'rules'，value_json 只存"与默认值不同"的项。
    未配置时排课行为 = rules.DEFAULT_RULES（即系统原有内定规则）。"""
    id: int = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)
    value_json: str = "{}"
    updated_at: str = ""


class MonthlyFeedback(SQLModel, table=True):
    """月度反馈行（出科考核反馈 / 月考反馈）的汇报人，导出时带出。
    kind 固定为「出科考核反馈」「月考反馈」两种；reporter 可为空（暂留空，手动选择）。"""
    id: int = Field(default=None, primary_key=True)
    year: int = 0
    month: int = 0
    kind: str = ""            # 出科考核反馈 / 月考反馈
    reporter_type: str = ""   # teacher / student / ""（空=未指定）
    reporter_id: int = 0
    note: str = ""            # 备注/题目，可空


class DingTalkConfig(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    webhook_url: str = ""
    secret: str = ""
    enabled: bool = False
    keywords: str = "课程表"  # 机器人安全设置的关键词（逗号分隔），推送时自动包含第一个
    app_key: str = ""         # 企业内部应用 AppKey（用于创建待办提醒，可空）
    app_secret: str = ""      # 企业内部应用 AppSecret
    todo_enabled: bool = False  # 发布课表时自动创建待办；并作为定时提醒总开关
    todo_base_url: str = ""     # 系统对外访问地址（如 http://192.168.1.10:8000），用于待办 detailUrl 跳转填报
    todo_lead_days: int = 1     # （旧字段，保留兼容）提前提醒天数
    reminder_days: str = "10,7,3"  # 多次提醒：课程开始前 N 天各提醒一次，逗号分隔，如 10,7,3
    bot_enabled: bool = False   # 启用群机器人（Stream 模式）：老师群里 @机器人 直接填报课程名
    calendar_enabled: bool = False  # 发布课表时把每节课同步为钉钉日历日程（老师手机日历可见）
    event_duration: int = 60        # 每节课时长（分钟），用于日程起止时间
    event_reminder_min: int = 15    # 日程开始前 N 分钟提醒（0 = 不提醒）
    card_enabled: bool = False      # 启用互动卡片（按钮点击走 Stream 回调，无需系统对外可达）
    card_template_id: str = ""      # 钉钉卡片平台「卡片模板 ID」，形如 xxxx.schema


class DingTalkWebhook(SQLModel, table=True):
    """其他钉钉群的自定义机器人：发布总课表时同步推送，后台可按群启用/停用。
    主群配置仍在 DingTalkConfig；每个群各自添加机器人，各自持有 Webhook / 加签 / 关键词。"""
    id: int = Field(default=None, primary_key=True)
    name: str = ""            # 群名称（便于后台辨认，如「教研室群」）
    webhook_url: str = ""     # https://oapi.dingtalk.com/robot/send?access_token=xxx
    secret: str = ""          # 加签密钥（SEC 开头，可空）
    keywords: str = "课程表"  # 该机器人安全设置的关键词（逗号分隔，取第一个）
    enabled: bool = False     # 启用推送（后台随时停用，停用后不再向该群发送）


class ReminderSent(SQLModel, table=True):
    """定时提醒发送记录：每（课程×教师×提前天数）只发一次，防止重启/重复触发。"""
    id: int = Field(default=None, primary_key=True)
    session_id: int = Field(index=True)
    teacher_id: int = Field(index=True)
    lead_days: int = 0
    sent_at: str = ""  # ISO 时间


class TodoSent(SQLModel, table=True):
    """安排待办创建记录：每（年月×教师）一条，重复发布时按内容指纹决定跳过或重建。"""
    id: int = Field(default=None, primary_key=True)
    year: int = Field(index=True, default=0)
    month: int = Field(index=True, default=0)
    teacher_id: int = Field(index=True, default=0)
    digest: str = ""     # 待办正文指纹，内容未变则跳过
    task_id: str = ""    # 钉钉待办 ID，内容变化时用于删除旧待办
    sent_at: str = ""    # ISO 时间


class EventSent(SQLModel, table=True):
    """钉钉日程同步记录：每节课一条，重复发布时按内容指纹跳过/更新，课程删除时删日程。"""
    id: int = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, default=0)
    year: int = 0
    month: int = 0
    digest: str = ""        # 日程内容指纹（标题+时间+参与人），未变则跳过
    event_id: str = ""      # 钉钉日程 ID
    organizer_uid: str = ""  # 日程所属人 unionId（更新/删除时必须用同一个）
    sent_at: str = ""


class CardAck(SQLModel, table=True):
    """互动卡片按钮确认记录：一张卡片（outTrackId）里谁在什么时候点了哪个按钮。
    卡片点击的回传请求走 Stream 长连接，不需要系统有公网/对内可达地址。"""
    id: int = Field(default=None, primary_key=True)
    out_track_id: str = Field(index=True, default="")
    userid: str = Field(index=True, default="")
    name: str = ""
    action: str = ""        # ack / fill（对应模板里按钮的 actionId）
    created_at: str = ""


class OperationLog(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    user: str = ""
    action: str = ""
    detail: str = ""
    time: str = ""


class RotationState(SQLModel, table=True):
    """轮转状态（按角色持久化，跨月连续轮转）：
    - ptr：该角色在候选池中的下一个轮转位置（全局均匀轮转，保证每人每月至多一次）
    - covered_json：已覆盖集合 {课型: [成员id]}，用于"三种课型全覆盖"的次级偏置
    """
    id: int = Field(default=None, primary_key=True)
    role: str = ""            # teacher / review / student（唯一）
    ptr: int = 0              # 下一个轮转位置
    covered_json: str = "{}"  # {"小讲课": [ids], "病例讨论": [ids], "教学阅片": [ids]}


# ===================== 技能考核考务 =====================
class ExamPlan(SQLModel, table=True):
    """一次技能考核（多站 OSCE）场次。排程结果以 tick 为单位（相对开考时间的分钟数）。"""
    id: int = Field(default=None, primary_key=True)
    name: str = ""              # 如「2026年07月放射规培基地出科考核」
    exam_date: str = ""         # YYYY-MM-DD
    start_time: str = "12:30"   # 开考时间 HH:MM
    tick_min: int = 10          # 时间格长度（分钟），各站时长须为其整数倍
    gap_min: int = 1            # 换场间隔（分钟）
    duty_min: int = 0           # 每位监考连续值守时长；0 = 整场不换人
    split_size: int = 0         # 每场人数；0 = 不分场
    auto_scale: bool = False    # 瓶颈自动扩容（默认关：新流程先按每站 1 人出草稿，再人工加考位）
    expand_penalty: int = 20    # 扩容取舍：每多派 1 名监考折算多少分钟（越大越倾向老师最少）
    status: str = "draft"       # draft / published
    note: str = ""
    created_at: str = ""


class ExamStation(SQLModel, table=True):
    """考站配置：时长、平行考位数（A/B…）、每站监考人数、地点。"""
    id: int = Field(default=None, primary_key=True)
    plan_id: int = Field(index=True, default=0)
    seq: int = 1                # 站序号 1..5
    name: str = "第一站"
    duration_min: int = 10
    rooms: int = 1              # 平行考位数（同时可考人数）
    teachers: int = 1           # 该站同一时刻需要的监考人数
    elastic: bool = False       # 弹性考站：机位不设上限（电脑考试），排程时迁就其他考站填隙
    auto_rooms: bool = False    # 自动扩容站：考位数/监考数按「考生人数 ÷ 目标时长」推导（人盯人站）
    align: bool = False         # 时间对齐：同站考生尽量集中在相同时间块（整点/半小时起考）
    location: str = ""
    enabled: bool = True
    color: str = ""
    teacher_ids: str = ""       # 人工指定的本站考官（老师 id 逗号分隔），留空 = 由系统按权重派


class ExamCandidate(SQLModel, table=True):
    """考生：从学生名册勾选产生，可只考部分考站。"""
    id: int = Field(default=None, primary_key=True)
    plan_id: int = Field(index=True, default=0)
    student_id: int = Field(index=True, default=0)
    name: str = ""
    group_name: str = ""        # 组别，如「影像三年级 B组」
    stations: str = ""          # 参加的站序号，逗号分隔，如 "1,2,3"；空 = 全考
    sort: int = 0
    is_out: bool = True         # 是否出科（勾选）


class ExamSlot(SQLModel, table=True):
    """排程结果：某考生在某考位某时段的占用。start/end 为相对开考时间的分钟数。"""
    id: int = Field(default=None, primary_key=True)
    plan_id: int = Field(index=True, default=0)
    station_seq: int = 0
    room: int = 0               # 0 = A 考位
    start_min: int = 0
    end_min: int = 0
    candidate_id: int = Field(index=True, default=0)
    batch: int = 0              # 第几场（split_size>0 时有效）


class ExamDuty(SQLModel, table=True):
    """监考安排：某考位某时段的监考老师。"""
    id: int = Field(default=None, primary_key=True)
    plan_id: int = Field(index=True, default=0)
    station_seq: int = 0
    room: int = 0
    start_min: int = 0
    end_min: int = 0
    teacher_id: int = 0
    teacher_name: str = ""
    teachers: str = ""          # 该时段全部监考姓名，逗号分隔（1 站多人时用）
    teacher_ids: str = ""       # 同上，老师 id 逗号分隔（发布推送用）
    batch: int = 0
    push_event_id: str = ""     # 已推送的钉钉日程 ID（重复推送时更新用）
    push_todo_id: str = ""      # 已推送的钉钉待办 ID
