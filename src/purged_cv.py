# -*- coding: utf-8 -*-
"""purged_cv.py -- 回测过拟合统计检验工具链 (PurgedKFold / CPCV / DSR / PBO / MinTRL).

定位
----
把 Lopez de Prado, "Advances in Financial Machine Learning" (2018) 与
Bailey & Lopez de Prado (2014) 里几件"回测可信度"工具集中到一处:

    PurgedKFold                       带 purge + embargo 的 K 折(sklearn 兼容接口)
    CombinatorialPurgedCV             CPCV: 组合式 purged 交叉验证, 生成多条回测路径
    deflated_sharpe_ratio             DSR: 按试验次数收缩后的 Sharpe 显著性
    probabilistic_sharpe_ratio        PSR: 单个 Sharpe 的显著性(DSR 的 N=1 特例)
    probability_backtest_overfitting  CSCV-PBO: 回测过拟合概率
    min_track_record_length           MinTRL: 达到统计显著所需的最少观测数
    sharpe_ratio                      年化/单期 Sharpe(与 pbo_cscv.sharpe 数值一致)

为什么需要 purge / embargo
--------------------------
金融标签通常是"区间标签": 样本 i 在 t0_i 观测, 但标签要到 t1_i 才确定
(例如 triple-barrier 的持有期)。若 [t0_i, t1_i] 与测试区间重叠, 该训练样本的
标签就"偷看"了测试期的价格, CV 分数被系统性高估。purge 把这类样本剔出训练集;
embargo 再额外剔除测试集之后的一小段样本, 用于切断序列相关(波动率聚集、特征
计算窗口跨过边界)留下的残余泄露。两者都是"宁可少用样本, 不可泄露信息"。

Sharpe 口径(全文统一, 重要)
---------------------------
- `sharpe_ratio(x, annualize=True)` 默认返回**年化** Sharpe(乘以 sqrt(trading_days))。
- `probabilistic_sharpe_ratio` / `deflated_sharpe_ratio` / `min_track_record_length`
  的入参 `sr`(以及 `benchmark` / `target_sr`)一律按**年化** Sharpe 解释,
  函数内部显式除以 sqrt(trading_days) 换成单期后再代入公式(公式本身定义在单期口径上)。
  这样调用方不必猜: 传进来的就是平时看的那个年化数。
- 返回 dict 里凡是单期口径的量(`sr0`、`sigma_sr`、`var_sr_trials`)在 docstring
  与 `scale` 字段里都写明; 年化口径的量(`e_max_sr`)同样写明。

依赖与副作用
------------
- 纯计算: 不读写文件, 不访问网络, 不使用随机数, 不修改入参。
- 只依赖标准库 + numpy。**不依赖 scipy / pandas**(正态 CDF/分位数用 math.erf
  自行实现)。pandas 只作为"可选输入类型"被鸭子类型接受(有 .index / .to_numpy 即可)。
- 唯一的外部引用是本仓既有的 src/pbo_cscv.py: 在 probability_backtest_overfitting
  内部延迟 import 以复用其 CSCV 实现, 保证与仓库既有口径数值一致; import 失败时
  自动退化到本模块内数值等价的 _cscv_pbo_local。

数值稳定性约定(不静默返回 0)
-----------------------------
- 样本不足 / 零方差 / 常量序列 -> 返回 NaN(sharpe_ratio), 或抛 ValueError(方差量)。
- NaN / NaT / inf 入参 -> 抛 ValueError, 不做静默填补或丢弃。
- 显著性函数遇到"数学上不可能"的方差(<=0) -> 抛 ValueError, 而不是返回一个假的 0。
各函数 docstring 里逐条列明。
"""
from __future__ import annotations

import itertools
import math
from typing import Iterator, List, Optional, Sequence, Tuple

import numpy as np

TRADING_DAYS = 252

# Euler-Mascheroni 常数(期望最大 Sharpe 的近似公式里出现)
_EULER_GAMMA = 0.5772156649015328606
_SQRT2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)

# 与 pbo_cscv.sharpe 保持完全一致的判据阈值: 少于 3 个有效观测或 std <= 1e-12 视为不可计算
_MIN_OBS_FOR_STD = 3
_ZERO_STD_EPS = 1e-12

# embargo 个数取 floor(pct * n) 时吸收浮点误差(0.29 * 100 = 28.999999999999996)
_EMBARGO_FLOOR_EPS = 1e-9

# purge 时按块做向量化比较, 控制峰值内存(块大小 x 测试集大小 的布尔矩阵)
_PURGE_CHUNK = 4096


# ==========================================================================
# 标准正态分布(不引入 scipy)
# ==========================================================================
def norm_cdf(x: float) -> float:
    """标准正态 CDF: Phi(x) = 0.5 * (1 + erf(x / sqrt(2))).

    为什么不用 scipy: 本模块只需要一个 CDF 和一个分位数, math.erf 已经提供
    机器精度级别的 erf, 引入 scipy 只为这一件事不划算(部署体积/版本冲突)。
    x = NaN -> NaN;  x = +-inf -> 1 / 0(显式处理, 避免 erf(inf) 的边界意外)。
    """
    x = float(x)
    if math.isnan(x):
        return float("nan")
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


# Acklam 有理逼近系数(初始值精度约 1.15e-9, 再经 Halley 迭代到双精度)
_ACKLAM_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
             1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_ACKLAM_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
             6.680131188771972e+01, -1.328068155288572e+01)
_ACKLAM_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
             -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_ACKLAM_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
             3.754408661907416e+00)
_ACKLAM_PLOW = 0.02425
_ACKLAM_PHIGH = 1.0 - _ACKLAM_PLOW


def norm_ppf(p: float) -> float:
    """标准正态分位数(逆 CDF): Phi^-1(p).

    实现: Acklam 有理逼近给初值 + 2 次 Halley 迭代, 精度 ~1e-15(远优于本模块
    所需的 1e-6)。迭代用 norm_cdf 与解析 pdf, 因此结果与 norm_cdf 自洽 ——
    测试里用 Phi(Phi^-1(p)) == p 交叉验证。

    p <= 0 -> -inf;  p >= 1 -> +inf;  p 非有限或落在 [0,1] 之外 -> ValueError。
    """
    p = float(p)
    if math.isnan(p):
        raise ValueError("norm_ppf: p 不能是 NaN")
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    if p < _ACKLAM_PLOW:                       # 左尾
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3])
              * q + _ACKLAM_C[4]) * q + _ACKLAM_C[5]) / \
            ((((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1.0)
    elif p <= _ACKLAM_PHIGH:                   # 中部
        q = p - 0.5
        r = q * q
        x = (((((_ACKLAM_A[0] * r + _ACKLAM_A[1]) * r + _ACKLAM_A[2]) * r + _ACKLAM_A[3])
              * r + _ACKLAM_A[4]) * r + _ACKLAM_A[5]) * q / \
            (((((_ACKLAM_B[0] * r + _ACKLAM_B[1]) * r + _ACKLAM_B[2]) * r + _ACKLAM_B[3])
              * r + _ACKLAM_B[4]) * r + 1.0)
    else:                                      # 右尾(用对称性, 避免大 p 处精度损失)
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3])
               * q + _ACKLAM_C[4]) * q + _ACKLAM_C[5]) / \
            ((((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1.0)

    for _ in range(2):                         # Halley 迭代(三阶收敛)
        e = norm_cdf(x) - p
        if e == 0.0:
            break
        u = e * _SQRT_2PI * math.exp(x * x / 2.0)
        x -= u / (1.0 + x * u / 2.0)
    return float(x)


# ==========================================================================
# 通用校验工具
# ==========================================================================
def _as_int(value, name: str) -> int:
    """把"应为整数"的参数转成 int; 非整数(含 NaN/inf/bool)一律 ValueError.

    为什么不用 int(value): int(2.7) 会静默截断成 2, 把用户的口径错误变成
    一个看起来正常的数字, 这正是本模块要避免的"静默"。
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数, 收到 bool: {value!r}")
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)) and math.isfinite(float(value)) \
            and float(value).is_integer():
        return int(value)
    raise ValueError(f"{name} 必须是整数, 收到 {value!r}")


def _as_finite_float(value, name: str) -> float:
    """转成有限浮点数; 非数值 / NaN / inf 一律 ValueError(不静默当作 0)."""
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是实数, 收到 {value!r}") from exc
    if not math.isfinite(v):
        raise ValueError(f"{name} 必须是有限实数, 收到 {value!r}")
    return v


def _as_positive_float(value, name: str) -> float:
    v = _as_finite_float(value, name)
    if v <= 0.0:
        raise ValueError(f"{name} 必须 > 0, 收到 {value!r}")
    return v


def _num_samples(X) -> int:
    """样本数 = len(X); 支持 numpy 数组 / list / pandas DataFrame 等."""
    try:
        n = len(X)
    except TypeError as exc:
        raise ValueError(f"X 必须支持 len()(numpy 数组 / 序列 / DataFrame), 收到 {type(X)!r}") from exc
    if n <= 0:
        raise ValueError(f"X 的样本数必须 > 0, 收到 {n}")
    return int(n)


def _as_1d(values) -> np.ndarray:
    """把标签区间(time 或 position)转成一维 numpy 数组, 尽量保留原生 dtype.

    优先 .to_numpy()(pandas Series 的 datetime64 会被正确保留), 否则 asarray。
    object dtype 的 datetime 再尝试转成 datetime64[ns], 这样与 DatetimeIndex
    的比较才不会退化成 Python 逐元素比较。
    """
    if hasattr(values, "to_numpy"):
        arr = np.asarray(values.to_numpy())
    else:
        arr = np.asarray(values)
    if arr.ndim != 1:
        arr = arr.ravel()
    if arr.dtype == object:
        try:
            arr = arr.astype("datetime64[ns]")
        except (TypeError, ValueError):
            pass
    return arr


def _is_missing_scalar(v) -> bool:
    """NaN / NaT / None 的通用判定: v != v 对 float('nan')、numpy NaT、pandas NaT 均为真."""
    if v is None:
        return True
    try:
        return bool(v != v)
    except Exception:      # pragma: no cover - 只对异常对象走这里
        return False


def _has_missing(arr: np.ndarray) -> bool:
    """一维数组里是否存在 NaN / NaT / None."""
    kind = arr.dtype.kind
    if kind in "fc":
        return bool(np.isnan(arr).any())
    if kind == "mM":
        return bool(np.isnat(arr).any())
    if kind == "O":
        return any(_is_missing_scalar(v) for v in arr.tolist())
    return False


# ==========================================================================
# 标签区间与 purge / embargo
# ==========================================================================
def _default_t0(X, n: int) -> np.ndarray:
    """训练/测试样本的"观测时间" t0.

    规则(写死, 避免隐式魔法):
    - X 带 .index(numpy 没有, pandas 有) -> 用 np.asarray(X.index).
      默认 RangeIndex 时它天然等于 0..n-1, DatetimeIndex 时是 datetime64,
      因此"位置口径"与"时间口径"共用同一段代码。
    - 否则 -> np.arange(n)(位置口径)。

    于是 t1 必须与 t0 同尺度: 数值 t1 被解释为"标签结束位置"(如 i + horizon),
    datetime t1 被解释为"标签结束时刻"。尺度不匹配时在比较处抛 ValueError。
    """
    idx = getattr(X, "index", None)
    if idx is not None:
        try:
            if len(idx) == n:
                return np.asarray(idx)
        except TypeError:                       # pragma: no cover - 奇怪的 index 对象
            pass
    return np.arange(n, dtype=np.float64)


def _extract_t1_from_y(y):
    """y 是 DataFrame(或任何有列访问的对象)时取 't1' 列; 否则返回 None."""
    if y is None:
        return None
    if hasattr(y, "__getitem__"):
        try:
            return y["t1"]
        except (KeyError, IndexError, TypeError):
            return None
    return None


def _resolve_label_intervals(X, y, t1) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """解析标签区间, 返回 (t0, t1) 或 (None, None) 表示"无区间信息".

    优先级: 显式 t1 参数 > y['t1']。两者都没有 -> (None, None), 调用方退化为
    "仅 embargo"(即只切掉测试集之后的一小段, 不做重叠剔除)。

    校验: 长度必须等于样本数; 含 NaN/NaT/None 直接 ValueError —— 标签结束时间
    未知就无法判定是否重叠, 静默当成"不重叠"会泄露, 静默当成"重叠"会白扔样本,
    两种静默都不可接受, 所以要求调用方先清理数据。
    """
    series = t1 if t1 is not None else _extract_t1_from_y(y)
    if series is None:
        return None, None
    n = _num_samples(X)
    arr = _as_1d(series)
    if arr.shape[0] != n:
        raise ValueError(f"t1 长度 {arr.shape[0]} 与样本数 {n} 不一致")
    if _has_missing(arr):
        raise ValueError("t1 含 NaN/NaT/None: 标签区间未知, 无法判定 purge; "
                         "本模块不做静默填补, 请先清理这些样本")
    return _default_t0(X, n), arr


def _purge_overlapping(t0: Optional[np.ndarray], t1: Optional[np.ndarray],
                       train: np.ndarray, test: np.ndarray) -> np.ndarray:
    """剔除标签区间与任一测试样本区间重叠的训练样本.

    判据(闭区间, 端点相切也算重叠): [t0_i, t1_i] 与 [t0_j, t1_j] 重叠
    <=> t0_i <= t1_j 且 t0_j <= t1_i。

    为什么用逐对判据而不是"测试集跨度 [min t0, max t1]"这种简化:
    CPCV 的测试集是若干个不相邻的块, 跨度会覆盖中间的训练块, 从而把大量
    本不重叠的样本误剔(过度 purge)。逐对判据是精确定义, 用分块向量化
    控制内存(峰值 ~ _PURGE_CHUNK x len(test) 的布尔矩阵)。
    """
    if t0 is None or t1 is None:
        return train
    tr = np.asarray(train, dtype=np.int64)
    te = np.asarray(test, dtype=np.int64)
    if tr.size == 0 or te.size == 0:
        return tr
    tt0 = t0[te]
    tt1 = t1[te]
    keep = np.ones(tr.size, dtype=bool)
    for s in range(0, tr.size, _PURGE_CHUNK):
        blk = tr[s:s + _PURGE_CHUNK]
        b0 = t0[blk][:, None]
        b1 = t1[blk][:, None]
        try:
            overlap = (b0 <= tt1[None, :]) & (tt0[None, :] <= b1)
        except TypeError as exc:
            raise ValueError(
                "标签区间尺度不匹配: t0 与 t1 无法比较(典型原因: X 是普通 numpy 数组, "
                "t0 退化为位置 0..n-1, 而 t1 是 datetime; 或 X 带 DatetimeIndex 而 t1 "
                "是数值位置)。请让 t0/t1 同为位置或同为时间") from exc
        keep[s:s + _PURGE_CHUNK] = ~overlap.any(axis=1)
    return tr[keep]


def _embargo_count(n: int, embargo_pct: float) -> int:
    """embargo 样本数 = floor(embargo_pct * n), 用 eps 吸收浮点噪声.

    为什么要 eps: 0.29 * 100 在二进制浮点里是 28.999999999999996, 直接 int() 会
    得到 28, 比用户按十进制手算的 29 少一个。加 1e-9 只影响"恰好整数"的情形,
    仍然是 floor 语义(向下取整), 不会多剔一个。
    """
    if embargo_pct <= 0.0:
        return 0
    return int(math.floor(embargo_pct * n + _EMBARGO_FLOOR_EPS))


def _embargo_filter(train: np.ndarray, test: np.ndarray, n_embargo: int) -> np.ndarray:
    """剔除测试集**之后**紧邻的 n_embargo 个样本(按位置口径).

    只剔测试集之后的样本, 这是 AFML 的定义: embargo 针对"测试期结束后的
    序列相关尾巴"; 测试集之前的样本只可能通过标签区间重叠泄露, 那由 purge 管。
    因此当测试块本身就在样本末尾时, 不会有任何样本被 embargo 掉(不是 bug)。
    """
    if n_embargo <= 0 or train.size == 0 or test.size == 0:
        return train
    test_max = int(np.max(test))
    lo = test_max + 1
    hi = test_max + 1 + n_embargo            # 半开区间 [lo, hi)
    return train[(train < lo) | (train >= hi)]


def _split_test_blocks(n: int, n_splits: int) -> List[np.ndarray]:
    """连续 K 折的测试块(与 sklearn.model_selection.KFold 一致: 前 n % k 折多一个样本)."""
    return [np.asarray(b, dtype=np.int64) for b in np.array_split(np.arange(n, dtype=np.int64), n_splits)]


def _validate_kfold_params(n_splits, embargo_pct) -> Tuple[int, float]:
    """公有切分器的公共参数校验(n_splits >= 2, embargo_pct 落在 [0, 1))."""
    k = _as_int(n_splits, "n_splits")
    if k < 2:
        raise ValueError(f"n_splits 必须 >= 2(至少要有训练与测试两部分), 收到 {n_splits!r}")
    p = _as_finite_float(embargo_pct, "embargo_pct")
    if not (0.0 <= p < 1.0):
        raise ValueError(f"embargo_pct 必须落在 [0, 1), 收到 {embargo_pct!r}")
    return k, p


# ==========================================================================
# PurgedKFold
# ==========================================================================
class PurgedKFold:
    """带 purge 与 embargo 的 K 折切分器(接口与 sklearn 的 splitter 兼容).

    参数
    ----
    n_splits : int, 默认 5. 折数, 必须 >= 2; 每折的测试集是一段**连续**样本块
               (金融时间序列不能随机打乱, 否则未来的信息会漏进训练集)。
    embargo_pct : float, 默认 0.01. 测试块之后按位置剔除 floor(embargo_pct * n)
               个样本, 必须落在 [0, 1)。
    t1 : 可选. 标签结束时间(长度 n 的一维数组, 或与 X.index 同尺度的 pandas Series)。
         也可以在 split() 里按次传入, 或把 y 传成带 't1' 列的 DataFrame。
         不给且 y 里也没有 't1' 时退化为"仅 embargo"(见下)。

    标签区间(决定 purge 是否发生)
    ----------------------------
    - t0 = X.index(普通 numpy 数组则退化为位置 0..n-1);
    - t1 由 split(t1=...)、构造参数 t1、或 y['t1'] 依次提供;
    - 训练样本 i 与任一测试样本 j 的 [t0, t1] 闭区间有重叠(t0_i <= t1_j 且
      t0_j <= t1_i, 端点相切算重叠)-> i 从训练集剔除;
    - 无标签区间信息时不做 purge, 只做 embargo。这是"显式退化": 用户没给区间,
      就只切掉测试块之后的一段, 绝不假装做了 purge。

    数值/边界行为(不静默)
    ----------------------
    - n_splits < 2、n_splits > n_samples、embargo_pct 不在 [0, 1) -> ValueError;
    - t1 长度与样本数不一致 -> ValueError;
    - t1 含 NaN/NaT/None -> ValueError(区间未知无法判定, 不静默填补);
    - t0/t1 尺度不匹配(位置 vs 时间)-> ValueError;
    - 训练集可能为空(极端 embargo + 极端 n_splits), 此时返回空数组而不报错,
      由调用方决定是否可用 —— 空训练集是"数据事实", 不是本模块的错。
    """

    def __init__(self, n_splits: int = 5, embargo_pct: float = 0.01, t1=None):
        self.n_splits, self.embargo_pct = _validate_kfold_params(n_splits, embargo_pct)
        self.t1 = t1
        self._n_samples: Optional[int] = None

    # ---------------------------------------------------------------- 接口
    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        """折数(与 sklearn 约定一致: 返回 split() 会产出多少组 (train, test))."""
        return self.n_splits

    def split(self, X, y=None, groups=None, t1=None) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
        """产出 (train_idx, test_idx). 索引是 numpy int64 数组, 升序、互斥.

        参数 X 只用来取样本数 n 与时间轴 X.index; y 用于取 y['t1'](可选);
        groups 未使用(仅为 sklearn 接口兼容而保留)。
        参数校验在调用 split() 时立即执行(不等到迭代), 便于快速失败。
        """
        n = _num_samples(X)
        if self.n_splits > n:
            raise ValueError(f"n_splits={self.n_splits} 不能大于样本数 n={n}")
        t0, t1v = _resolve_label_intervals(X, y, t1 if t1 is not None else self.t1)
        self._n_samples = n
        return self._generate(n, t0, t1v)

    # ---------------------------------------------------------------- 内部
    def _generate(self, n: int, t0, t1v) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
        n_embargo = _embargo_count(n, self.embargo_pct)
        all_idx = np.arange(n, dtype=np.int64)
        for test in _split_test_blocks(n, self.n_splits):
            mask = np.ones(n, dtype=bool)
            mask[test] = False
            train = all_idx[mask]
            train = _purge_overlapping(t0, t1v, train, test)
            train = _embargo_filter(train, test, n_embargo)
            yield np.asarray(train, dtype=np.int64), np.asarray(test, dtype=np.int64)

    def __repr__(self) -> str:      # pragma: no cover - 仅便于调试
        return (f"PurgedKFold(n_splits={self.n_splits}, embargo_pct={self.embargo_pct}, "
                f"t1={'given' if self.t1 is not None else None})")


# ==========================================================================
# CombinatorialPurgedCV
# ==========================================================================
class CombinatorialPurgedCV:
    """CPCV: 把样本切成 n_splits 个连续块, 每次取 n_test_splits 块做测试.

    与 PurgedKFold 的关系: 切分粒度从"折"变成"块组合", 每个组合测试 k 个块、
    训练其余块(同样做 purge + embargo)。C(n, k) 个组合不是 C(n, k) 条独立回测,
    而是能拼出 backtest_paths(n, k) = C(n, k) * k / n 条**完整路径**:
    每个块会作为测试集出现在 C(n-1, k-1) 个组合里, 把这些组合的测试期预测按
    时间顺序拼接, 就得到一条覆盖全样本的 OOS 路径。这正是 CPCV 的价值所在 ——
    用同一份历史得到多条 OOS 路径, 从而能估计"路径之间的分散度", 而不像单次
    walk-forward 只有一条路径、无法区分"策略好"与"这段历史好"。

    参数
    ----
    n_splits : int, 默认 6. 连续块数, 必须 >= 2。
    n_test_splits : int, 默认 2. 每次取几个块做测试, 必须落在 [1, n_splits - 1]
                   (取 n_splits 会让训练集为空, 没有意义)。
    embargo_pct : float, 默认 0.01. 同 PurgedKFold。
    t1 : 可选, 标签结束时间, 语义同 PurgedKFold(也可在 split() 里按次传, 或由 y['t1'] 提供)。

    公开接口
    --------
    split(X, y=None, groups=None, t1=None) -> 生成器, 逐个组合产出 (train_idx, test_idx)
    get_n_splits(...) -> C(n_splits, n_test_splits), 即 split() 会产出多少组
    backtest_paths(n_splits, n_test_splits) -> int, 回测路径条数(静态方法, 整数运算)
    combinations() -> 测试块组合列表, 顺序与 split() 的产出顺序一致
    assign_groups(n_samples=None, X=None) -> 每个样本所属的测试块编号(0..n_splits-1)
    n_paths -> 属性, 等于 backtest_paths(n_splits, n_test_splits)

    数值/边界行为: 与 PurgedKFold 相同的 ValueError 约定; 另外
    n_test_splits 不在 [1, n_splits-1] -> ValueError。
    """

    def __init__(self, n_splits: int = 6, n_test_splits: int = 2,
                 embargo_pct: float = 0.01, t1=None):
        self.n_splits, self.embargo_pct = _validate_kfold_params(n_splits, embargo_pct)
        k = _as_int(n_test_splits, "n_test_splits")
        if not (1 <= k < self.n_splits):
            raise ValueError(f"n_test_splits 必须落在 [1, n_splits-1] = [1, {self.n_splits - 1}], "
                             f"收到 {n_test_splits!r}")
        self.n_test_splits = k
        self.t1 = t1
        self.n_paths = self.backtest_paths(self.n_splits, self.n_test_splits)
        self._n_samples: Optional[int] = None

    # ---------------------------------------------------------------- 路径数
    @staticmethod
    def backtest_paths(n_splits, n_test_splits) -> int:
        """回测路径条数 = C(n, k) * k / n, 用整数运算精确求值.

        为什么必须走整数: C(10,3)*3/10 = 120*3/10, 浮点除法会得到 36.000000000000004
        这类值, 一旦参与 "路径条数" 这种计数用途就会埋雷。这里先算分子
        C(n, k) * k, 断言能整除 n, 再整除。
        数学上恒有 C(n, k) * k / n == C(n-1, k-1)(整数), 断言是防御性的:
        万一有人把公式改错, 会立刻炸而不是给出一个错的小数。
        """
        n = _as_int(n_splits, "n_splits")
        k = _as_int(n_test_splits, "n_test_splits")
        if n < 2:
            raise ValueError(f"n_splits 必须 >= 2, 收到 {n_splits!r}")
        if not (1 <= k <= n):
            raise ValueError(f"n_test_splits 必须落在 [1, n_splits] = [1, {n}], 收到 {n_test_splits!r}")
        numerator = math.comb(n, k) * k
        if numerator % n != 0:      # pragma: no cover - 数学上不可能发生
            raise ValueError(f"C({n},{k}) * {k} 不能被 {n} 整除, 参数组合异常")
        return numerator // n

    # ---------------------------------------------------------------- 接口
    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        """split() 会产出多少组 (train, test) = C(n_splits, n_test_splits) 个组合.

        注意这**不是**回测路径条数: 路径条数由 backtest_paths() / n_paths 给出
        (= 组合数 * k / n, 通常远小于组合数)。
        """
        return math.comb(self.n_splits, self.n_test_splits)

    def combinations(self) -> List[Tuple[int, ...]]:
        """测试块组合的列表(升序元组), 顺序与 split() 的产出顺序一致."""
        return list(itertools.combinations(range(self.n_splits), self.n_test_splits))

    def split(self, X, y=None, groups=None, t1=None) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
        """逐个测试块组合产出 (train_idx, test_idx)(purge + embargo 同 PurgedKFold)."""
        n = _num_samples(X)
        if self.n_splits > n:
            raise ValueError(f"n_splits={self.n_splits} 不能大于样本数 n={n}")
        t0, t1v = _resolve_label_intervals(X, y, t1 if t1 is not None else self.t1)
        self._n_samples = n
        return self._generate(n, t0, t1v)

    def assign_groups(self, n_samples=None, X=None) -> np.ndarray:
        """每个样本属于哪个测试块(0..n_splits-1), 与 split() 的分块口径**完全一致**.

        实现直接用 _split_test_blocks(即 np.array_split: 不能整除时前 n % k 块多一个
        样本), 而不是 floor(i * k / n) 这类"看起来等价"的公式 —— 后者在 n 不能被 k
        整除时会与 split() 的块边界错位(本模块的测试专门盯住这一点)。

        用途: 把各组合的 OOS 表现重组回完整回测路径 —— 先按块编号拿到"每个样本
        在哪个组合里是测试样本", 再按时间串起来。
        n_samples 未给且 X 未给时, 用最近一次 split() 的样本数;
        两者都没有(还没 split 过)-> ValueError, 因为无从推断样本数。
        """
        if X is not None:
            n = _num_samples(X)
        elif n_samples is not None:
            n = _as_int(n_samples, "n_samples")
            if n <= 0:
                raise ValueError(f"n_samples 必须 > 0, 收到 {n_samples!r}")
        elif self._n_samples is not None:
            n = self._n_samples
        else:
            raise ValueError("assign_groups 需要 n_samples 或 X(或先调用一次 split(X))")
        groups = np.empty(n, dtype=np.int64)
        for b, idx in enumerate(_split_test_blocks(n, self.n_splits)):
            groups[idx] = b
        return groups

    # ---------------------------------------------------------------- 内部
    def _generate(self, n: int, t0, t1v) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
        n_embargo = _embargo_count(n, self.embargo_pct)
        all_idx = np.arange(n, dtype=np.int64)
        blocks = _split_test_blocks(n, self.n_splits)
        for combo in itertools.combinations(range(self.n_splits), self.n_test_splits):
            test = np.sort(np.concatenate([blocks[b] for b in combo]))
            mask = np.ones(n, dtype=bool)
            mask[test] = False
            train = all_idx[mask]
            train = _purge_overlapping(t0, t1v, train, test)
            train = _embargo_filter(train, test, n_embargo)
            yield np.asarray(train, dtype=np.int64), np.asarray(test, dtype=np.int64)

    def __repr__(self) -> str:      # pragma: no cover - 仅便于调试
        return (f"CombinatorialPurgedCV(n_splits={self.n_splits}, "
                f"n_test_splits={self.n_test_splits}, embargo_pct={self.embargo_pct}, "
                f"n_combos={self.get_n_splits()}, n_paths={self.n_paths})")


# ==========================================================================
# Sharpe / PSR / DSR / MinTRL
# ==========================================================================
def sharpe_ratio(x, annualize: bool = True, trading_days: float = TRADING_DAYS) -> float:
    """收益序列的 Sharpe(无风险利率取 0).

    计算: mean / std(ddof=1), 非年化口径; annualize=True 时再乘 sqrt(trading_days)。
    ddof=1(样本标准差)与 pbo_cscv.sharpe 完全一致 —— 这是刻意的: 本模块的 PBO
    要与仓库既有实现数值对齐, 度量口径不能差一点点。

    明确行为(不静默返回 0):
    - 丢弃 NaN/inf 后有效观测 < 3 -> NaN;
    - 标准差 <= 1e-12(常量序列/零波动)或非有限 -> NaN;
    - 空输入 -> NaN。
    为什么零波动返回 NaN 而不是 0 或 inf: SR 在零波动时数学上未定义(0/0 或 x/0),
    返回 0 会让"常量收益"看起来像一个平庸但有效的策略, 掩盖数据问题。
    """
    a = np.asarray(x, dtype=np.float64).ravel()
    a = a[np.isfinite(a)]
    if a.size < _MIN_OBS_FOR_STD:
        return float("nan")
    sd = float(np.std(a, ddof=1))
    if not np.isfinite(sd) or sd <= _ZERO_STD_EPS:
        return float("nan")
    sr = float(np.mean(a) / sd)
    if annualize:
        td = _as_positive_float(trading_days, "trading_days")
        return float(sr * math.sqrt(td))
    return sr


def _psr_z(sr_annual: float, benchmark_annual: float, n_obs: int,
           skew: float, kurtosis: float, trading_days: float) -> Tuple[float, float, float, float]:
    """PSR 的 z 统计量与相关中间量.

    返回 (z, var_pp, sigma_pp, sr_pp):
        sr_pp    = sr_annual / sqrt(trading_days)          # 单期 Sharpe
        var_pp   = (1 + 0.5*sr_pp^2 - skew*sr_pp + (kurtosis-3)/4*sr_pp^2) / (n_obs-1)
        sigma_pp = sqrt(var_pp)
        z        = (sr_pp - benchmark_pp) / sigma_pp

    这里用的是 Sharpe 估计量的**渐近方差**(Mertens 2002 / Lo 2002 的标准形式,
    也即 Bailey & Lopez de Prado 用的那条):
        Var(SR_hat) ~= (1 - skew*SR + (kurtosis-1)/4*SR^2) / (n-1)
    两种写法等价: 1 + 0.5*SR^2 - skew*SR + (kurtosis-3)/4*SR^2
                = 1 - skew*SR + (kurtosis-1)/4*SR^2。
    本模块统一采用前一种(与任务书给出的式子逐字一致), docstring 在这里写清,
    免得日后有人对不上公式。
    方差 <= 0(极端 skew/kurtosis 组合下的病态输入)-> ValueError: 这种输入下
    显著性无从谈起, 返回任何一个具体数字都是误导。
    """
    n_obs = _as_int(n_obs, "n_obs")
    if n_obs < 2:
        raise ValueError(f"n_obs 必须 >= 2(否则方差无定义), 收到 {n_obs!r}")
    td = _as_positive_float(trading_days, "trading_days")
    scale = math.sqrt(td)
    sr_pp = _as_finite_float(sr_annual, "sr") / scale
    b_pp = _as_finite_float(benchmark_annual, "benchmark") / scale
    var_pp = (1.0 + 0.5 * sr_pp ** 2 - skew * sr_pp
              + (kurtosis - 3.0) / 4.0 * sr_pp ** 2) / (n_obs - 1.0)
    if not math.isfinite(var_pp) or var_pp <= 0.0:
        raise ValueError(f"Sharpe 估计量方差非正({var_pp!r}): 检查 skew/kurtosis/sr 是否合理")
    sigma_pp = math.sqrt(var_pp)
    z = (sr_pp - b_pp) / sigma_pp
    return z, var_pp, sigma_pp, sr_pp


def probabilistic_sharpe_ratio(sr, n_obs, skew: float = 0.0, kurtosis: float = 3.0,
                               benchmark: float = 0.0,
                               trading_days: float = TRADING_DAYS) -> float:
    """PSR: P(真实 Sharpe > benchmark), 即 z 统计量的标准正态 CDF.

    口径: `sr`、`benchmark` 都是**年化** Sharpe, 内部除以 sqrt(trading_days) 转单期。
    skew/kurtosis 是收益率的偏度与峰度(峰度用"原始峰度", 正态 = 3, 不是超额峰度)。
    返回 Phi(z) ∈ (0, 1), 不是百分数。

    它解决什么问题: 只看"Sharpe = 2"无法判断这是真本事还是样本少 + 收益有偏
    (负偏 + 厚尾会把 Sharpe 的标准误放大)。PSR 把这两件事折进 z 的分母。

    行为: n_obs < 2 或方差非正 -> ValueError; sr/benchmark/skew/kurtosis 非有限 -> ValueError。
    """
    z, _, _, _ = _psr_z(sr, benchmark, n_obs, _as_finite_float(skew, "skew"),
                        _as_finite_float(kurtosis, "kurtosis"), trading_days)
    return float(norm_cdf(z))


def deflated_sharpe_ratio(sr, n_trials, n_obs, skew: float = 0.0, kurtosis: float = 3.0,
                          sr_variance: Optional[float] = None,
                          trading_days: float = TRADING_DAYS) -> dict:
    """DSR: 对"试了 N 个配置, 挑最好的那个"这件事做收缩后的 Sharpe 显著性.

    口径(写死, 调用前不用猜)
    ------------------------
    - `sr` 按**年化** Sharpe 解释, 内部除以 sqrt(trading_days) 换成单期后再代入公式
      (公式本身定义在单期口径上)。`sr_variance` 与 `sr` 同口径(年化 Sharpe 的方差),
      内部同样除以 trading_days: Var(年化) = trading_days * Var(单期)。
    - 返回 dict 里 `sr0`、`sigma_sr`、`var_sr_trials` 是**单期**口径; `e_max_sr` 是
      **年化**口径(= sr0 * sqrt(trading_days)); `scale` 字段把这个约定也写进去了。
    - `dsr == psr` 恒成立: DSR 的定义就是"把 PSR 的基准从 0 换成期望最大 Sharpe"。
      两个键都留着是为了让调用方按论文里的叫法取用。

    公式(Bailey & Lopez de Prado 2014)
    -----------------------------------
    1) 期望最大 Sharpe(选择偏差的基准):
           sr0 = sqrt(Var(sr_trials)) * ((1 - gamma) * Z^-1(1 - 1/N)
                                        + gamma * Z^-1(1 - 1/(N*e)))
       其中 gamma 是 Euler-Mascheroni 常数(0.5772...), e 是自然常数, N = n_trials。
       来源: N 个独立同分布试验的最大值的期望(极值理论近似)。
       Var(sr_trials) 是"单次试验 Sharpe 估计量的方差": N 个试验的 Sharpe 是同一
       真值周围的 N 次带噪估计, 所以试验间的横截面方差就等于单次估计量的方差。
    2) Var(sr_trials) 的默认近似(docstring 明确在此):
           var = (1 + 0.5*sr^2 - skew*sr + (kurtosis-3)/4*sr^2) / (n_obs - 1)
       即 Sharpe 估计量的渐近方差(单期口径)。选它的原因: 手头只有一条被选中的
       收益序列时, 无法可靠估出"试验间"方差; 这条解析式只需要 sr 与收益的高阶矩。
       若调用方真的保留了全部 N 条试验序列, 应把实测方差通过 `sr_variance` 传进来
       (比默认近似更可信, 也是论文推荐顺序)。
    3) z = (sr_pp - sr0) / sigma,  sigma^2 = (1 + 0.5*sr_pp^2 - skew*sr_pp
                                            + (kurtosis-3)/4*sr_pp^2) / (n_obs - 1)
       DSR = Phi(z)。注意分母始终用上面这条解析方差(标准 PSR), 与 `sr_variance`
       无关 —— `sr_variance` 只影响 sr0 的收缩尺度。默认情况下两者是同一个数,
       因此默认路径上没有任何隐藏歧义。

    特例
    ----
    - n_trials = 1: 公式里会出现 Z^-1(1 - 1/1) = Z^-1(0) = -inf, 数学上"最大值"
      退化为唯一那个试验, 没有选择偏差。因此显式取 sr0 = 0, DSR 退化为 PSR(0)。
    - sr_variance = 0(试验间零方差, 例如所有试验就是同一条序列): sr0 = 0, 不做收缩。

    行为: n_obs < 2 / n_trials < 1 / sr_variance < 0 / 任一入参非有限 -> ValueError。

    返回 dict 键
    ------------
    dsr, sr0, psr, z, e_max_sr, n_trials, n_obs (任务书要求的 7 个)
    + sr_pp, sr, skew, kurtosis, trading_days, var_sr_trials, sigma_sr, sigma_trials, scale
    """
    N = _as_int(n_trials, "n_trials")
    if N < 1:
        raise ValueError(f"n_trials 必须 >= 1, 收到 {n_trials!r}")
    n_obs_i = _as_int(n_obs, "n_obs")
    if n_obs_i < 2:
        raise ValueError(f"n_obs 必须 >= 2(否则方差无定义), 收到 {n_obs!r}")
    sr_f = _as_finite_float(sr, "sr")
    skew_f = _as_finite_float(skew, "skew")
    kurt_f = _as_finite_float(kurtosis, "kurtosis")
    td = _as_positive_float(trading_days, "trading_days")
    scale = math.sqrt(td)
    sr_pp = sr_f / scale

    # --- 单次试验 Sharpe 估计量的方差(单期口径): 决定 sr0 的收缩尺度
    # _psr_z 返回 (z, var_pp, sigma_pp, sr_pp) —— 这里要的是 var/sigma, 不是 z
    _, var_psr, sigma_psr, _ = _psr_z(sr_f, 0.0, n_obs_i, skew_f, kurt_f, td)
    if sr_variance is None:
        var_trials_pp = var_psr
    else:
        sv = _as_finite_float(sr_variance, "sr_variance")
        if sv < 0.0:
            raise ValueError(f"sr_variance 必须 >= 0, 收到 {sr_variance!r}")
        var_trials_pp = sv / td
    sigma_trials_pp = math.sqrt(var_trials_pp)

    # --- 期望最大 Sharpe(单期口径)
    if N == 1 or var_trials_pp == 0.0:
        sr0_pp = 0.0
    else:
        q1 = norm_ppf(1.0 - 1.0 / N)
        q2 = norm_ppf(1.0 - 1.0 / (N * math.e))
        sr0_pp = sigma_trials_pp * ((1.0 - _EULER_GAMMA) * q1 + _EULER_GAMMA * q2)

    z = (sr_pp - sr0_pp) / sigma_psr
    dsr = float(norm_cdf(z))
    e_max_sr = sr0_pp * scale

    return {
        "dsr": dsr,
        "sr0": float(sr0_pp),                 # 单期口径的基准 Sharpe
        "psr": dsr,                           # DSR == PSR(sr0), 按定义相同
        "z": float(z),
        "e_max_sr": float(e_max_sr),          # 年化口径的期望最大 Sharpe
        "n_trials": N,
        "n_obs": n_obs_i,
        "sr_pp": float(sr_pp),
        "sr": float(sr_f),
        "skew": float(skew_f),
        "kurtosis": float(kurt_f),
        "trading_days": float(td),
        "var_sr_trials": float(var_trials_pp),   # 单期口径
        "sigma_sr": float(sigma_psr),            # 单期口径的 PSR 标准差
        "sigma_trials": float(sigma_trials_pp),  # 单期口径的试验间标准差(供 sr0 用)
        "scale": "sr/e_max_sr 年化; sr0/sigma_sr/sigma_trials/var_sr_trials 单期",
    }


def min_track_record_length(sr, target_sr: float = 0.0, skew: float = 0.0,
                            kurtosis: float = 3.0, prob: float = 0.95,
                            trading_days: float = TRADING_DAYS) -> float:
    """MinTRL: 要让 Sharpe 显著高于 target_sr, 至少需要多少观测(Bailey 的 MinTRL).

    公式(单期口径):
        MinTRL = 1 + sigma_num^2 * (Z^-1(prob) / (SR - SR_target))^2
        sigma_num^2 = 1 - skew*SR + (kurtosis-1)/4*SR^2
    口径与 DSR 一致: `sr`、`target_sr` 都是**年化** Sharpe, 内部除以
    sqrt(trading_days) 转单期; 因此 (SR - SR_target) 的比值与年化/单期无关,
    只有 sigma_num^2 需要单期口径。返回 float, 取 ceil 即"最少观测数"。

    为什么需要它: DSR 回答"这次回测的 Sharpe 有多可信", MinTRL 回答"这条记录
    还要跑多久才够可信"。两者共用同一个方差近似, 所以结论互相自洽。

    行为(明确, 不静默):
    - sr <= target_sr -> 返回 +inf: 期望 Sharpe 不高于目标时, 再多观测也不可能
      让它"显著更高", 这是数学事实(不是 0, 也不是一个很大的有限数);
    - prob 必须落在 (0.5, 1); 否则 ValueError(prob <= 0.5 的 Z^-1 <= 0, 公式没有
      置信意义);
    - 任一入参非有限 / trading_days <= 0 -> ValueError。
    """
    p = _as_finite_float(prob, "prob")
    if not (0.5 < p < 1.0):
        raise ValueError(f"prob 必须落在 (0.5, 1)(置信水平), 收到 {prob!r}")
    td = _as_positive_float(trading_days, "trading_days")
    scale = math.sqrt(td)
    sr_pp = _as_finite_float(sr, "sr") / scale
    tgt_pp = _as_finite_float(target_sr, "target_sr") / scale
    skew_f = _as_finite_float(skew, "skew")
    kurt_f = _as_finite_float(kurtosis, "kurtosis")
    excess = sr_pp - tgt_pp
    if excess <= 0.0:
        return math.inf
    sigma_num_sq = 1.0 - skew_f * sr_pp + (kurt_f - 1.0) / 4.0 * sr_pp ** 2
    if not math.isfinite(sigma_num_sq) or sigma_num_sq <= 0.0:
        raise ValueError(f"方差不为正({sigma_num_sq!r}): 检查 skew/kurtosis/sr 是否合理")
    z = norm_ppf(p)
    return float(1.0 + sigma_num_sq * (z / excess) ** 2)


# ==========================================================================
# CSCV-PBO
# ==========================================================================
def _avg_rank(values: np.ndarray) -> np.ndarray:
    """升序平均排名(1..N, 并列取平均), 与 scipy.stats.rankdata 的 'average' 等价.

    与 pbo_cscv._avg_rank 逐行等价(含 NaN 排最后并共享末位平均名次的处理),
    这样本模块的兜底实现与仓库既有实现给出完全相同的 PBO。
    """
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(n, dtype=np.float64)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _annualized_sharpe(col) -> float:
    """PBO 的默认表现度量: 年化 Sharpe, 与 pbo_cscv.sharpe 数值一致."""
    return sharpe_ratio(col, annualize=True)


def _cscv_pbo_local(blocks: Sequence[np.ndarray], metric=_annualized_sharpe) -> dict:
    """CSCV-PBO 的本地实现(与 pbo_cscv.cscv_pbo 数值等价)。

    为什么还要有一份: probability_backtest_overfitting 优先复用仓库既有实现,
    但本模块要能在 pbo_cscv 不可 import(独立使用/被裁剪部署)时照常工作。
    两份实现在同一输入上必须给出同一个 PBO —— 测试里对此做了交叉验证。

    与 pbo_cscv.cscv_pbo 保持一致的关键点: 组合枚举顺序、平均排名、
    w = rank / (N + 1)、lambda = logit(w)、PBO = P(lambda < 0)、
    IS 全 NaN 的组合跳过、以及"全部组合都不可计算 -> ValueError"。
    """
    if not blocks:
        raise ValueError("blocks 不能为空")
    arrs = [np.asarray(b, dtype=np.float64) for b in blocks]
    if any(a.ndim != 2 for a in arrs):
        raise ValueError("每个 block 必须是二维 (天数, 配置数)")
    S = len(arrs)
    N = arrs[0].shape[1]
    if N < 2:
        raise ValueError(f"至少需要 2 个配置, 实际 {N}")
    if S < 4 or S % 2 != 0:
        raise ValueError(f"块数 S 必须为 >=4 的偶数, 实际 {S}")
    if any(a.shape[1] != N for a in arrs):
        raise ValueError("各 block 的配置数 N 不一致")

    half = S // 2
    lambdas, omegas, is_best_oos, is_best_is = [], [], [], []
    oos_loss = 0
    is_all, oos_all = [], []

    for is_idx in itertools.combinations(range(S), half):
        is_set = set(is_idx)
        oos_idx = [i for i in range(S) if i not in is_set]
        m_is = np.concatenate([arrs[i] for i in is_idx], axis=0)
        m_oos = np.concatenate([arrs[i] for i in oos_idx], axis=0)

        p_is = np.array([metric(m_is[:, n]) for n in range(N)], dtype=np.float64)
        p_oos = np.array([metric(m_oos[:, n]) for n in range(N)], dtype=np.float64)
        if not np.isfinite(p_is).any():
            continue
        n_star = int(np.nanargmax(p_is))

        ranks = _avg_rank(p_oos)
        w = float(ranks[n_star]) / (N + 1)
        lam = math.log(w / (1.0 - w))
        lambdas.append(lam)
        omegas.append(w)
        is_best_is.append(float(p_is[n_star]))
        is_best_oos.append(float(p_oos[n_star]))
        if np.isfinite(p_oos[n_star]) and p_oos[n_star] < 0:
            oos_loss += 1
        is_all.append(p_is)
        oos_all.append(p_oos)

    if not lambdas:
        raise ValueError("所有组合的 IS 表现均不可计算(检查收益矩阵是否全为 NaN)")

    lam = np.array(lambdas, dtype=np.float64)
    pbo = float(np.mean(lam < 0.0))

    slope = intercept = float("nan")
    X = np.concatenate(is_all) if is_all else np.array([])
    Y = np.concatenate(oos_all) if oos_all else np.array([])
    m = np.isfinite(X) & np.isfinite(Y)
    if m.sum() >= 3 and np.std(X[m]) > 1e-12:
        slope, intercept = np.polyfit(X[m], Y[m], 1)

    return {
        "pbo": pbo,
        "n_blocks": S,
        "n_configs": N,
        "n_combos": len(lambdas),
        "lambda_mean": float(np.mean(lam)),
        "lambda_std": float(np.std(lam, ddof=1)) if lam.size > 1 else 0.0,
        "lambda_p05": float(np.percentile(lam, 5)),
        "lambda_p50": float(np.percentile(lam, 50)),
        "lambda_p95": float(np.percentile(lam, 95)),
        "omega_mean": float(np.mean(omegas)),
        "prob_oos_loss": float(oos_loss / len(lambdas)),
        "is_best_mean_is": float(np.mean(is_best_is)),
        "is_best_mean_oos": float(np.mean(is_best_oos)),
        "is_oos_slope": float(slope),
        "is_oos_intercept": float(intercept),
    }


def probability_backtest_overfitting(returns, n_splits: int = 8, metric=None) -> dict:
    """CSCV 估计回测过拟合概率 PBO.

    输入
    ----
    returns : T x N 的日收益矩阵(T 天, N 个候选配置)或 pandas DataFrame。
              允许 NaN(度量函数内部按列丢弃非有限值)。
    n_splits : CSCV 的块数 S, 默认 8。必须为 >= 4 的**偶数**(S/2 与 S/2 对称划分),
               且 T >= S。行按时间顺序切成 S 个连续块(np.array_split, 允许各块不等长)。
    metric : callable(col) -> float, 作用在"某块拼接后的某一列"上, 默认年化 Sharpe。
             口径与 pbo_cscv.cscv_pbo 的 metric 完全一致(按列调用)。

    做法与判读
    ----------
    对每个"取 S/2 块作 IS, 其余 S/2 块作 OOS"的组合: 取 IS 上最优配置 n*, 记它在
    OOS 上的归一化排名 w = rank/(N+1), lambda = logit(w); PBO = P(lambda < 0),
    即"IS 最优配置在 OOS 掉进后半"的频率。PBO 越低越好: 高 PBO 意味着这个
    "挑最优配置"的过程大概率只是在拟合噪声。

    实现选择
    --------
    优先在函数内 `import pbo_cscv` 复用仓库既有实现(保证与仓库口径一致);
    import 失败时退回 _cscv_pbo_local(数值等价的本地实现)。返回 dict 里
    `source` 字段标明走的哪条路('pbo_cscv' / 'local')。

    数值/边界行为(明确)
    -------------------
    - n_splits 非偶数 / < 4 -> ValueError(CSCV 的定义要求对称划分);
    - returns 不是二维, 或 N < 2, 或 T < S -> ValueError;
    - 所有组合的 IS 度量都是 NaN(典型情形: 收益矩阵整列/整片 NaN)-> ValueError,
      而不是返回一个"看起来是 0"的 PBO;
    - 本函数**不**强制"每块至少几天": T == S(每块 1 天)时 IS 仍有 S/2 天可用,
      度量的统计意义由调用方负责 —— 这里不替调用方设统计门槛, 只保证不静默出错;
    - 返回 dict 含 pbo / n_blocks(=S) / n_configs / n_combos / lambda 统计 /
      prob_oos_loss / is_oos_slope 等, 另加 n_splits / n_obs / metric / source。
    """
    S = _as_int(n_splits, "n_splits")
    if S < 4 or S % 2 != 0:
        raise ValueError(f"CSCV 要求 n_splits 为 >= 4 的偶数, 收到 {n_splits!r}")
    if hasattr(returns, "to_numpy"):
        arr = np.asarray(returns.to_numpy(dtype=np.float64), dtype=np.float64)
    else:
        arr = np.asarray(returns, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"returns 必须是二维 (T, N) 收益矩阵, 收到 ndim={arr.ndim}")
    T, N = arr.shape
    if N < 2:
        raise ValueError(f"至少需要 2 个候选配置, 实际 {N}")
    if T < S:
        raise ValueError(f"样本天数 T={T} 少于块数 n_splits={S}, 每块会为空")

    blocks = np.array_split(arr, S, axis=0)
    used_metric = _annualized_sharpe if metric is None else metric
    if not callable(used_metric):
        raise ValueError(f"metric 必须是可调用对象, 收到 {metric!r}")

    pbo_cscv = None
    try:
        import pbo_cscv              # type: ignore[import-not-found]  # 仓库既有实现
    except Exception:                # 部署环境里没有该模块时走本地兜底(测试用 monkeypatch 覆盖)
        pbo_cscv = None
    if pbo_cscv is not None and hasattr(pbo_cscv, "cscv_pbo"):
        res = dict(pbo_cscv.cscv_pbo(blocks, metric=used_metric))
        res["source"] = "pbo_cscv"
    else:
        res = _cscv_pbo_local(blocks, used_metric)
        res["source"] = "local"
    res.update({
        "n_splits": S,
        "n_obs": int(T),
        "n_configs": int(N),
        "metric": getattr(used_metric, "__name__", repr(used_metric)),
    })
    return res


__all__ = [
    "TRADING_DAYS",
    "norm_cdf",
    "norm_ppf",
    "sharpe_ratio",
    "probabilistic_sharpe_ratio",
    "deflated_sharpe_ratio",
    "min_track_record_length",
    "probability_backtest_overfitting",
    "PurgedKFold",
    "CombinatorialPurgedCV",
]
