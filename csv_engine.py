"""
CSV 数据适配器
=============
用 pandas DataFrame 模拟 SQLAlchemy engine 的查询行为，
使得 agent_main.py 中的所有函数无需修改即可使用 CSV 文件作为数据源。

用法:
    from csv_engine import CsvEngine
    engine = CsvEngine("./env_huayao_tables")
    # engine 可以直接传给 agent_main.py 里所有接收 engine 参数的函数
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


class _Row:
    """模拟 SQLAlchemy Row，支持下标和字典访问。"""
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self._data.values())[key]
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()


class _ResultProxy:
    """模拟 SQLAlchemy Result 对象。"""
    def __init__(self, rows: List[dict]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return [_Row(r) for r in self._rows]

    def fetchall(self):
        return [tuple(r.values()) for r in self._rows]

    def scalar(self):
        if not self._rows:
            return None
        return list(self._rows[0].values())[0]


class _Connection:
    """模拟 SQLAlchemy Connection，拦截 execute() 调用并用 pandas 实现。"""
    def __init__(self, engine: "CsvEngine"):
        self.engine = engine

    def execute(self, sql_text, params=None):
        """解析 SQL 语义，用 pandas 实现等效查询。"""
        if params is None:
            params = {}

        sql = sql_text if isinstance(sql_text, str) else str(sql_text)
        sql_lower = sql.lower().strip()

        # ---- 路由到具体查询实现 ----
        if "from staff" in sql_lower and "name_search_cache" in sql_lower:
            return self._query_staff_by_name(params)

        if "from students" in sql_lower and "name_search_cache" in sql_lower:
            return self._query_student_by_name(params)

        if "from topics" in sql_lower:
            return self._query_all_topics()

        if "distinct teacher_id" in sql_lower and "from subjects" in sql_lower and "topic_id" in sql_lower:
            return self._query_teachers_for_topics(params)

        if "from staff" in sql_lower and "id in" in sql_lower:
            return self._query_staff_by_ids(params)

        if "teacher_id from subjects" in sql_lower and "class_id" in sql_lower:
            return self._query_teacher_for_class(params)

        if "distinct student_id" in sql_lower and "from student_classes" in sql_lower:
            return self._query_students_for_class(params)

        if "distinct topic_id" in sql_lower and "from subjects" in sql_lower:
            return self._query_topics_for_class(params)

        if "count(*)" in sql_lower and "from lessons" in sql_lower:
            return self._query_lesson_count(sql_lower, params)

        if "from lessons" in sql_lower and "student_classes" in sql_lower:
            return self._query_student_lessons(sql_lower, params)

        if "from lessons" in sql_lower:
            return self._query_teacher_lessons(sql_lower, params)

        # fallback
        return _ResultProxy([])

    # ──────────── 具体查询实现 ────────────

    def _query_staff_by_name(self, params):
        name = params.get("name", "")
        df = self.engine.staff
        if df.empty or "name_search_cache" not in df.columns:
            return _ResultProxy([])
        match = df[df["name_search_cache"] == name]
        if match.empty:
            return _ResultProxy([])
        return _ResultProxy([{"id": int(match.iloc[0]["id"])}])

    def _query_student_by_name(self, params):
        name = params.get("name", "")
        df = self.engine.students
        if df.empty or "name_search_cache" not in df.columns:
            return _ResultProxy([])
        match = df[df["name_search_cache"] == name]
        if match.empty:
            return _ResultProxy([])
        return _ResultProxy([{"id": int(match.iloc[0]["id"])}])

    def _query_all_topics(self):
        # 没有 topics.csv → 从 subjects + classes 合成
        df = self.engine.topics
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "id": int(r["id"]),
                "name": r.get("name", ""),
                "cn_name": r.get("cn_name", ""),
            })
        return _ResultProxy(rows)

    def _query_teachers_for_topics(self, params):
        topic_ids = params.get("topic_ids", ())
        if isinstance(topic_ids, (list, tuple)):
            topic_ids = set(int(x) for x in topic_ids)
        else:
            topic_ids = {int(topic_ids)}
        df = self.engine.subjects
        match = df[df["topic_id"].isin(topic_ids) & df["teacher_id"].notna()]
        teacher_ids = match["teacher_id"].dropna().unique()
        return _ResultProxy([{"teacher_id": int(tid)} for tid in teacher_ids])

    def _query_staff_by_ids(self, params):
        ids = params.get("ids", ())
        if isinstance(ids, (list, tuple)):
            ids = set(int(x) for x in ids)
        else:
            ids = {int(ids)}
        df = self.engine.staff
        match = df[df["id"].isin(ids)]
        rows = []
        for _, r in match.iterrows():
            rows.append({"id": int(r["id"]), "name": r["name_search_cache"]})
        return _ResultProxy(rows)

    def _query_teacher_for_class(self, params):
        cid = int(params.get("cid", 0))
        df = self.engine.subjects
        match = df[df["class_id"] == cid]
        if match.empty:
            return _ResultProxy([])
        return _ResultProxy([{"teacher_id": int(match.iloc[0]["teacher_id"])}])

    def _query_students_for_class(self, params):
        cid = int(params.get("cid", 0))
        df = self.engine.student_classes
        match = df[df["class_id"] == cid]
        sids = match["student_id"].unique()
        return _ResultProxy([{"student_id": int(s)} for s in sids])

    def _query_topics_for_class(self, params):
        cid = int(params.get("cid", 0))
        df = self.engine.subjects
        match = df[(df["class_id"] == cid) & (df["topic_id"].notna()) & (df["topic_id"] != 0)]
        tids = match["topic_id"].unique()
        return _ResultProxy([{"topic_id": int(t)} for t in tids])

    def _query_lesson_count(self, sql_lower, params):
        """COUNT(*) 查询 — 用于 is_group_free"""
        start_unix = int(params.get("start_unix", 0))
        end_unix = int(params.get("end_unix", 0))

        lessons = self.engine.lessons
        subjects = self.engine.subjects

        # 合并 lessons + subjects
        merged = lessons.merge(subjects, left_on="subject_id", right_on="id", suffixes=("", "_subj"))

        # 时间过滤
        merged = merged[
            ((merged["start_time"] == -1) | (merged["start_time"] <= end_unix)) &
            ((merged["end_time"] == -1) | (merged["end_time"] >= start_unix))
        ]

        if "student_classes" in sql_lower:
            # 学生冲突查询
            sids = params.get("sids", ())
            if isinstance(sids, (list, tuple)):
                sids = set(int(x) for x in sids)
            else:
                sids = {int(sids)}
            sc = self.engine.student_classes
            # lessons → subjects → classes → student_classes
            merged2 = merged.merge(
                sc[sc["student_id"].isin(sids)],
                left_on="class_id",
                right_on="class_id",
                suffixes=("", "_sc"),
            )
            return _ResultProxy([{"count": len(merged2)}])
        else:
            # 老师冲突查询
            tid = int(params.get("tid", 0))
            filtered = merged[merged["teacher_id"] == tid]
            return _ResultProxy([{"count": len(filtered)}])

    def _time_filter(self, lessons_df, start_unix, end_unix):
        return lessons_df[
            ((lessons_df["start_time"] == -1) | (lessons_df["start_time"] <= end_unix)) &
            ((lessons_df["end_time"] == -1) | (lessons_df["end_time"] >= start_unix))
        ]

    def _format_time(self, unix_ts):
        if unix_ts == -1:
            return "PERMANENT"
        try:
            return datetime.fromtimestamp(int(unix_ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(unix_ts)

    def _query_student_lessons(self, sql_lower, params):
        """学生课表查询 — JOIN lessons, subjects, classes, student_classes, [topics], [staff]"""
        start_unix = int(params.get("start_unix", 0))
        end_unix = int(params.get("end_unix", 0))
        student_id = params.get("student_id") or params.get("sid")
        if student_id is not None:
            student_id = int(student_id)

        lessons = self.engine.lessons
        subjects = self.engine.subjects
        classes = self.engine.classes
        sc = self.engine.student_classes
        staff = self.engine.staff
        topics = self.engine.topics

        # 时间过滤 lessons
        filtered_lessons = self._time_filter(lessons, start_unix, end_unix)

        # JOIN subjects
        merged = filtered_lessons.merge(
            subjects[["id", "class_id", "teacher_id", "topic_id"]],
            left_on="subject_id", right_on="id", suffixes=("", "_subj"),
        )

        # JOIN classes
        merged = merged.merge(
            classes[["id", "class_type"]],
            left_on="class_id", right_on="id", suffixes=("", "_cls"),
        )

        # JOIN student_classes (with time filter)
        sc_filtered = sc.copy()
        if student_id is not None:
            sc_filtered = sc_filtered[sc_filtered["student_id"] == student_id]
        # enrollment time filter
        sc_filtered = sc_filtered[
            ((sc_filtered["start_time"] == -1) | (sc_filtered["start_time"] <= end_unix)) &
            ((sc_filtered["end_time"] == -1) | (sc_filtered["end_time"] >= start_unix))
        ]
        merged = merged.merge(
            sc_filtered[["class_id", "student_id", "start_time", "end_time"]],
            on="class_id", suffixes=("", "_sc"),
        )

        # LEFT JOIN topics
        if not topics.empty:
            merged = merged.merge(
                topics[["id", "name", "cn_name"]],
                left_on="topic_id", right_on="id", how="left", suffixes=("", "_topic"),
            )
        else:
            merged["name_topic"] = ""
            merged["cn_name"] = ""

        # LEFT JOIN staff (for teacher_name)
        has_staff_join = "staff" in sql_lower or "teacher_name" in sql_lower or "stf." in sql_lower
        if has_staff_join:
            merged = merged.merge(
                staff[["id", "name_search_cache"]],
                left_on="teacher_id", right_on="id", how="left", suffixes=("", "_staff"),
            )

        rows = []
        for _, r in merged.iterrows():
            lesson_start_unix = r["start_time"]
            lesson_end_unix = r["end_time"]
            row = {
                "lesson_id": int(r.iloc[0]),  # lessons.id
                "lesson_start_unix": int(lesson_start_unix),
                "lesson_end_unix": int(lesson_end_unix),
                "lesson_start": self._format_time(lesson_start_unix),
                "lesson_end": self._format_time(lesson_end_unix),
                "subject_id": int(r["id_subj"]) if "id_subj" in r.index else int(r["subject_id"]),
                "class_id": int(r["class_id"]),
                "teacher_id": int(r["teacher_id"]),
                "class_name": r.get("class_type", ""),
                "topic_name": r.get("name_topic", r.get("name", "")) if not topics.empty else "",
                "topic_cn_name": r.get("cn_name", "") if not topics.empty else "",
            }
            if "sc_start_time" in sql_lower or "sc.start_time" in sql_lower:
                row["sc_start_time"] = int(r.get("start_time_sc", r.get("start_time", -1)))
                row["sc_end_time"] = int(r.get("end_time_sc", r.get("end_time", -1)))
            if has_staff_join:
                row["teacher_name"] = r.get("name_search_cache", "")
            rows.append(row)

        return _ResultProxy(rows)

    def _query_teacher_lessons(self, sql_lower, params):
        """老师课表查询 — JOIN lessons, subjects, classes, [topics]"""
        start_unix = int(params.get("start_unix", 0))
        end_unix = int(params.get("end_unix", 0))
        teacher_id = params.get("teacher_id") or params.get("tid")
        if teacher_id is not None:
            teacher_id = int(teacher_id)

        lessons = self.engine.lessons
        subjects = self.engine.subjects
        classes = self.engine.classes
        topics = self.engine.topics
        sc = self.engine.student_classes
        students = self.engine.students

        # 时间过滤 lessons
        filtered_lessons = self._time_filter(lessons, start_unix, end_unix)

        # JOIN subjects
        merged = filtered_lessons.merge(
            subjects[["id", "class_id", "teacher_id", "topic_id"]],
            left_on="subject_id", right_on="id", suffixes=("", "_subj"),
        )

        # 过滤老师
        if teacher_id is not None:
            merged = merged[merged["teacher_id"] == teacher_id]

        # JOIN classes
        merged = merged.merge(
            classes[["id", "class_type"]],
            left_on="class_id", right_on="id", suffixes=("", "_cls"),
        )

        # LEFT JOIN topics
        if not topics.empty:
            merged = merged.merge(
                topics[["id", "name", "cn_name"]],
                left_on="topic_id", right_on="id", how="left", suffixes=("", "_topic"),
            )

        # 是否需要查 student_classes（附带学生列表）
        need_students = "student_classes" in sql_lower or "sc." in sql_lower

        rows = []
        for _, r in merged.iterrows():
            lesson_start_unix = r["start_time"]
            lesson_end_unix = r["end_time"]
            row = {
                "lesson_id": int(r.iloc[0]),
                "lesson_start_unix": int(lesson_start_unix),
                "lesson_end_unix": int(lesson_end_unix),
                "lesson_start": self._format_time(lesson_start_unix),
                "lesson_end": self._format_time(lesson_end_unix),
                "subject_id": int(r["id_subj"]) if "id_subj" in r.index else int(r["subject_id"]),
                "class_id": int(r["class_id"]),
                "teacher_id": int(r["teacher_id"]),
                "class_name": r.get("class_type", ""),
                "topic_name": r.get("name_topic", r.get("name", "")) if not topics.empty else "",
                "topic_cn_name": r.get("cn_name", "") if not topics.empty else "",
            }
            rows.append(row)

        return _ResultProxy(rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class CsvEngine:
    """
    用 CSV 文件模拟 SQLAlchemy engine。

    用法:
        engine = CsvEngine("./env_huayao_tables")
        # 然后把 engine 传给 agent_main.py 中的任何函数
    """
    def __init__(self, csv_dir: str):
        self.csv_dir = csv_dir
        print(f"[CsvEngine] 从 {csv_dir} 加载数据...")

        self.staff = self._load("staff.csv")
        self.students = self._load("students.csv")
        self.classes = self._load("classes.csv")
        self.subjects = self._load("subjects.csv")
        self.lessons = self._load("lessons.csv")
        self.student_classes = self._load("student_classes.csv")

        # topics 表可能不存在 → 从 subjects + classes 合成
        topics_path = os.path.join(csv_dir, "topics.csv")
        if os.path.exists(topics_path):
            self.topics = self._load("topics.csv")
        else:
            self.topics = self._build_synthetic_topics()

        self._convert_types()

        print(f"[CsvEngine] 加载完成: "
              f"staff={len(self.staff)}, students={len(self.students)}, "
              f"classes={len(self.classes)}, subjects={len(self.subjects)}, "
              f"lessons={len(self.lessons)}, student_classes={len(self.student_classes)}, "
              f"topics={len(self.topics)}")

    def _load(self, filename: str) -> pd.DataFrame:
        path = os.path.join(self.csv_dir, filename)
        if not os.path.exists(path):
            print(f"  [警告] 未找到 {path}，返回空 DataFrame")
            return pd.DataFrame()
        return pd.read_csv(path, dtype=str, keep_default_na=False)

    def _convert_types(self):
        """把关键的 ID / 时间戳列转成 int"""
        int_cols = {
            "staff": ["id"],
            "students": ["id"],
            "classes": ["id", "topic_id", "teacher_id"],
            "subjects": ["id", "class_id", "teacher_id", "topic_id"],
            "lessons": ["id", "subject_id", "start_time", "end_time"],
            "student_classes": ["id", "student_id", "class_id", "start_time", "end_time"],
            "topics": ["id"],
        }
        for table_name, cols in int_cols.items():
            df = getattr(self, table_name)
            if df.empty:
                continue
            for col in cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    def _build_synthetic_topics(self) -> pd.DataFrame:
        """没有 topics.csv 时，从 classes 表中提取 topic_id 并用 class name 作为 topic name"""
        if self.classes.empty or self.subjects.empty:
            return pd.DataFrame(columns=["id", "name", "cn_name"])

        # 从 subjects 拿所有 topic_id，再从 classes 找对应名称
        subj = self.subjects.copy()
        subj["topic_id_num"] = pd.to_numeric(subj["topic_id"], errors="coerce").fillna(0).astype(int)
        subj = subj[subj["topic_id_num"] > 0]

        cls = self.classes.copy()
        cls["id_num"] = pd.to_numeric(cls["id"], errors="coerce").fillna(0).astype(int)
        cls["topic_id_num"] = pd.to_numeric(cls["topic_id"], errors="coerce").fillna(0).astype(int)

        # 每个 topic_id 取第一个 class name 作为标识
        topic_rows = {}
        for _, r in cls.iterrows():
            tid = int(r["topic_id_num"])
            if tid > 0 and tid not in topic_rows:
                cname = r.get("name", r.get("search_cache", ""))
                # 尝试提取学科关键字
                topic_rows[tid] = {"id": tid, "name": cname, "cn_name": ""}

        if not topic_rows:
            return pd.DataFrame(columns=["id", "name", "cn_name"])

        return pd.DataFrame(list(topic_rows.values()))

    def connect(self):
        """模拟 engine.connect()，返回一个上下文管理器。"""
        return _Connection(self)
