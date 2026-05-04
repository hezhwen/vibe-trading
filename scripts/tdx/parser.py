"""
TDX .day 文件解析器

格式: 32字节 Little-endian, struct "<IIIIIfII"
  [0] uint32  日期 (YYYYMMDD)
  [1] uint32  开盘价（分 × 0.01 = 元）
  [2] uint32  最高价
  [3] uint32  最低价
  [4] uint32  收盘价
  [5] float32 成交额（元）
  [6] uint32  成交量（手或股，需验证）
  [7] uint32  填充

文件路径: vipdoc/{sh,sz,bj}/lday/{market}{code}.day
"""

from __future__ import annotations
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional
import datetime


@dataclass
class DayRecord:
    """单条日K数据"""
    date: int              # YYYYMMDD
    open: float           # 元
    high: float           # 元
    low: float            # 元
    close: float          # 元
    amount: float         # 成交额（元）
    volume: int           # 成交量（手）
    # 以下为计算字段
    pre_close: Optional[float] = None
    change_pct: Optional[float] = None


class TdxDayParser:
    """TDX .day 文件解析器
    
    Usage:
        parser = TdxDayParser('/path/to/vipdoc')
        
        # 解析单只股票
        for record in parser.parse('sz', '000001'):
            print(record.date, record.close)
        
        # 验证成交量单位
        unit = parser.verify_volume_unit('sz', '000001')
        print(f"Volume unit: {unit}")  # 'hand' or 'share'
    """
    
    HEADER_SIZE = 32
    FORMAT = '<IIIIIfII'  # Little-endian
    
    def __init__(self, vipdoc_path: str | Path):
        self.vipdoc_path = Path(vipdoc_path)
        
    def _get_file_path(self, market: str, code: str) -> Path:
        """获取 .day 文件路径
        
        Args:
            market: 'sh' | 'sz' | 'bj'
            code: 股票代码，如 '000001'
        """
        market_lower = market.lower()
        filename = f"{market_lower}{code}.day"
        return self.vipdoc_path / market_lower / 'lday' / filename
    
    def parse(self, market: str, code: str) -> Iterator[DayRecord]:
        """解析单只股票的 .day 文件
        
        Args:
            market: 'sh' | 'sz' | 'bj'
            code: 股票代码，如 '000001'
            
        Yields:
            DayRecord 按日期升序排列
        """
        filepath = self._get_file_path(market, code)
        if not filepath.exists():
            raise FileNotFoundError(f"文件不存在: {filepath}")
        
        with open(filepath, 'rb') as f:
            while True:
                data = f.read(self.HEADER_SIZE)
                if not data:
                    break
                    
                fields = struct.unpack(self.FORMAT, data)
                date = fields[0]
                record = DayRecord(
                    date=date,
                    open=fields[1] / 100.0,
                    high=fields[2] / 100.0,
                    low=fields[3] / 100.0,
                    close=fields[4] / 100.0,
                    amount=fields[5],
                    volume=fields[6],  # 原始值，需要verify_volume_unit确认单位
                )
                yield record
    
    def parse_date_range(
        self, market: str, code: str, 
        start_date: int, end_date: int
    ) -> Iterator[DayRecord]:
        """解析指定日期范围的日K数据
        
        Args:
            market: 'sh' | 'sz' | 'bj'
            code: 股票代码
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD
        """
        for record in self.parse(market, code):
            if record.date < start_date:
                continue
            if record.date > end_date:
                break
            yield record
    
    def verify_volume_unit(
        self, market: str, code: str, 
        expected_ratio_range: tuple = (0.95, 1.05)
    ) -> dict:
        """
        验证成交量单位
        
        验证方法: amount / (volume × 100 × close) ≈ 1.0
          ≈ 1.0 → volume 是"手"（hand）
          ≈ 0.01 → volume 是"股"（share），需要除以100
        
        Args:
            market: 'sh' | 'sz' | 'bj'
            code: 股票代码
            expected_ratio_range: 期望的ratio范围，默认(0.95, 1.05)
            
        Returns:
            dict: {
                'volume_unit': 'hand' | 'share',
                'correction_factor': 1 or 100,
                'ratios': [float],  # 所有记录的ratio列表
                'mean_ratio': float,
                'status': 'OK' | 'ERROR',
                'message': str
            }
        """
        records = list(self.parse(market, code))
        if not records:
            return {
                'volume_unit': 'unknown',
                'correction_factor': 1,
                'ratios': [],
                'mean_ratio': 0.0,
                'status': 'ERROR',
                'message': '无数据'
            }
        
        ratios = []
        for r in records:
            if r.close > 0 and r.volume > 0 and r.amount > 0:
                ratio = r.amount / (r.volume * 100 * r.close)
                ratios.append(ratio)
        
        if not ratios:
            return {
                'volume_unit': 'unknown',
                'correction_factor': 1,
                'ratios': [],
                'mean_ratio': 0.0,
                'status': 'ERROR',
                'message': '无法计算ratio（数据异常）'
            }
        
        mean_ratio = sum(ratios) / len(ratios)
        
        # 判断单位
        if expected_ratio_range[0] <= mean_ratio <= expected_ratio_range[1]:
            volume_unit = 'hand'
            correction_factor = 1
            status = 'OK'
            message = f'volume 是"手"（ratio={mean_ratio:.4f}）'
        elif 0.009 <= mean_ratio <= 0.011:
            volume_unit = 'share'
            correction_factor = 100
            status = 'OK'
            message = f'volume 是"股"，需除以100（ratio={mean_ratio:.4f}）'
        else:
            volume_unit = 'unknown'
            correction_factor = 1
            status = 'ERROR'
            message = f'ratio={mean_ratio:.4f}，超出预期范围'
        
        return {
            'volume_unit': volume_unit,
            'correction_factor': correction_factor,
            'ratios': ratios,
            'mean_ratio': mean_ratio,
            'status': status,
            'message': message
        }
    
    def get_stock_list(self, market: str) -> list[str]:
        """获取指定市场的股票代码列表
        
        Args:
            market: 'sh' | 'sz' | 'bj'
            
        Returns:
            股票代码列表，如 ['000001', '000002', ...]
        """
        lday_path = self.vipdoc_path / market.lower() / 'lday'
        if not lday_path.exists():
            return []
        
        codes = []
        prefix = market.lower()
        for f in lday_path.glob(f'{prefix}*.day'):
            code = f.stem[len(prefix):]  # 去掉前缀
            codes.append(code)
        return sorted(codes)


# 已知数据验证（用于验证解析器正确性）
KNOWN_DATA = {
    '平安银行': {
        'market': 'sz', 'code': '000001',
        'date': 20230103,
        'close': 12.85,  # 2023-01-03 收盘价约12.85
        'volume_hand': 8500000,  # 约850万手
        'amount_yuan': 10930000000,  # 约109亿元
    },
    '贵州茅台': {
        'market': 'sh', 'code': '600519',
        'date': 20230103,
        'close': 1855.00,  # 约1855元
        'volume_hand': 2600000,  # 约260万手
        'amount_yuan': 52000000000,  # 约52亿元
    }
}


def verify_parser(parser: TdxDayParser, tolerance: float = 0.05) -> dict:
    """
    使用已知数据验证解析器正确性
    
    Args:
        parser: TdxDayParser实例
        tolerance: 允许的误差（默认5%）
        
    Returns:
        dict: 验证结果
    """
    results = {}
    
    for name, expected in KNOWN_DATA.items():
        records = list(parser.parse(expected['market'], expected['code']))
        if not records:
            results[name] = {'status': 'ERROR', 'message': '无数据'}
            continue
        
        # 找最接近目标日期的记录
        target_date = expected['date']
        closest = min(records, key=lambda r: abs(r.date - target_date))
        
        close_diff = abs(closest.close - expected['close']) / expected['close']
        volume_diff = abs(closest.volume * 100 - expected['volume_hand']) / expected['volume_hand']
        amount_diff = abs(closest.amount - expected['amount_yuan']) / expected['amount_yuan']
        
        status = 'OK' if (
            close_diff < tolerance and 
            volume_diff < tolerance and 
            amount_diff < tolerance
        ) else 'WARNING'
        
        results[name] = {
            'status': status,
            'expected': expected,
            'actual': {
                'date': closest.date,
                'close': closest.close,
                'volume': closest.volume,
                'amount': closest.amount,
            },
            'diff': {
                'close': f'{close_diff:.2%}',
                'volume': f'{volume_diff:.2%}',
                'amount': f'{amount_diff:.2%}',
            }
        }
    
    return results


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) < 2:
        print("用法: python -m scripts.tdx.parser <vipdoc_path>")
        print("示例: python -m scripts.tdx.parser /root/github-code/Vibe-Trading/data/tdx/vipdoc")
        sys.exit(1)
    
    vipdoc_path = sys.argv[1]
    parser = TdxDayParser(vipdoc_path)
    
    print("=== TDX .day 解析器测试 ===\n")
    
    # 验证成交量单位
    print("1. 验证成交量单位 (sz000001 平安银行):")
    result = parser.verify_volume_unit('sz', '000001')
    print(f"   状态: {result['status']}")
    print(f"   单位: {result['volume_unit']}")
    print(f"   修正因子: {result['correction_factor']}")
    print(f"   平均ratio: {result['mean_ratio']:.6f}")
    print(f"   消息: {result['message']}")
    
    print("\n2. 验证成交量单位 (sh600519 贵州茅台):")
    result = parser.verify_volume_unit('sh', '600519')
    print(f"   状态: {result['status']}")
    print(f"   单位: {result['volume_unit']}")
    print(f"   修正因子: {result['correction_factor']}")
    print(f"   平均ratio: {result['mean_ratio']:.6f}")
    
    print("\n3. 解析示例 (sz000001 最近5条):")
    records = list(parser.parse('sz', '000001'))
    for r in records[-5:]:
        date_str = str(r.date)
        print(f"   {date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}: "
              f"O={r.open:.2f} H={r.high:.2f} L={r.low:.2f} C={r.close:.2f} "
              f"Vol={r.volume:,} Amt={r.amount/1e8:.2f}亿")
    
    print("\n4. 股票列表 (sh 前10个):")
    sh_list = parser.get_stock_list('sh')[:10]
    print(f"   {sh_list}")
    print(f"   sh 总数: {len(parser.get_stock_list('sh'))}")
    print(f"   sz 总数: {len(parser.get_stock_list('sz'))}")
    print(f"   bj 总数: {len(parser.get_stock_list('bj'))}")
