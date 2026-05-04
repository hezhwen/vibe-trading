"""
QFQ 前复权文件解析器

通达信导出的前复权数据格式:
- 文件名: SH#000001.txt, SZ#000001.txt, BJ#810011.txt
- 分隔符: Tab
- 第1行: 中文表头（可能有乱码）
- 第2行: 英文列名
- 第3行起: 数据

数据列:
  日期      开盘      最高      最低      收盘      成交量      成交额
  2015/01/05  7.94    8.14    7.67    7.96    286043584  4565386752.00
"""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional
import re


@dataclass
class QfqRecord:
    """前复权单条数据"""
    date: int              # YYYYMMDD
    open: float
    high: float
    low: float
    close: float
    volume: int           # 成交量（手）
    amount: float         # 成交额（元）


class QfqParser:
    """QFQ 前复权文件解析器
    
    Usage:
        parser = QfqParser('/path/to/export')
        
        # 解析单只股票
        for record in parser.parse('SH#000001'):
            print(record.date, record.close)
    """
    
    def __init__(self, export_path: str | Path):
        self.export_path = Path(export_path)
        # 列索引
        self._col_date = 0
        self._col_open = 1
        self._col_high = 2
        self._col_low = 3
        self._col_close = 4
        self._col_volume = 5
        self._col_amount = 6
    
    def parse(self, filename: str) -> Iterator[QfqRecord]:
        """解析前复权文件
        
        Args:
            filename: 文件名，如 'SH#000001' 或完整路径
            
        Yields:
            QfqRecord 按日期升序排列
        """
        filepath = Path(filename)
        if not filepath.exists():
            # 可能是相对文件名
            filepath = self.export_path / filename
        if not filepath.exists():
            raise FileNotFoundError(f"文件不存在: {filepath}")
        
        with open(filepath, 'r', encoding='gbk', errors='replace') as f:
            lines = f.readlines()
        
        # 跳过前两行（表头）
        data_lines = lines[2:] if len(lines) > 2 else []
        
        for line in data_lines:
            line = line.strip()
            if not line:
                continue
            
            fields = line.split('\t')
            if len(fields) < 7:
                continue
            
            try:
                # 解析日期 (2015/01/05 -> 20150105)
                date_str = fields[self._col_date].strip()
                date_int = self._parse_date(date_str)
                
                # 解析数值
                open_p = float(fields[self._col_open])
                high_p = float(fields[self._col_high])
                low_p = float(fields[self._col_low])
                close_p = float(fields[self._col_close])
                volume = int(fields[self._col_volume])
                amount = float(fields[self._col_amount])
                
                yield QfqRecord(
                    date=date_int,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=volume,
                    amount=amount,
                )
            except (ValueError, IndexError) as e:
                # 跳过无效行
                continue
    
    def _parse_date(self, date_str: str) -> int:
        """解析日期字符串"""
        # 支持格式: 2015/01/05, 2015-01-05, 20150105
        for fmt in ('%Y/%m/%d', '%Y-%m-%d'):
            try:
                from datetime import datetime
                dt = datetime.strptime(date_str, fmt)
                return dt.year * 10000 + dt.month * 100 + dt.day
            except ValueError:
                continue
        
        # 尝试直接转换
        return int(date_str.replace('/', '').replace('-', ''))
    
    def parse_symbol(self, market: str, code: str) -> Iterator[QfqRecord]:
        """解析指定市场的股票
        
        Args:
            market: 'SH' | 'SZ' | 'BJ'
            code: 股票代码，如 '000001'
        """
        filename = f"{market}#{code}.txt"
        yield from self.parse(filename)
    
    def get_stock_list(self) -> list[str]:
        """获取所有股票文件名列表"""
        if not self.export_path.exists():
            return []
        
        files = []
        for f in self.export_path.glob('*.txt'):
            files.append(f.stem)
        return sorted(files)
    
    def get_market_count(self) -> dict:
        """获取各市场的股票数量"""
        all_files = self.get_stock_list()
        counts = {'SH': 0, 'SZ': 0, 'BJ': 0}
        for f in all_files:
            if f.startswith('SH#'):
                counts['SH'] += 1
            elif f.startswith('SZ#'):
                counts['SZ'] += 1
            elif f.startswith('BJ#'):
                counts['BJ'] += 1
        return counts


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) < 2:
        print("用法: python -m scripts.tdx.qfq_parser <export_path>")
        print("示例: python -m scripts.tdx.qfq_parser /root/github-code/Vibe-Trading/data/qfq/export")
        sys.exit(1)
    
    export_path = sys.argv[1]
    parser = QfqParser(export_path)
    
    print("=== QFQ 前复权文件解析器测试 ===\n")
    
    print("1. 股票数量统计:")
    counts = parser.get_market_count()
    print(f"   SH: {counts['SH']}")
    print(f"   SZ: {counts['SZ']}")
    print(f"   BJ: {counts['BJ']}")
    print(f"   总计: {sum(counts.values())}")
    
    print("\n2. 解析示例 (SH#000001 最近5条):")
    try:
        records = list(parser.parse('SH#000001'))
        for r in records[-5:]:
            date_str = str(r.date)
            print(f"   {date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}: "
                  f"O={r.open:.2f} H={r.high:.2f} L={r.low:.2f} C={r.close:.2f} "
                  f"Vol={r.volume:,} Amt={r.amount/1e8:.2f}亿")
    except Exception as e:
        print(f"   错误: {e}")
    
    print("\n3. 解析示例 (SZ#000001 最近5条):")
    try:
        records = list(parser.parse('SZ#000001'))
        for r in records[-5:]:
            date_str = str(r.date)
            print(f"   {date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}: "
                  f"O={r.open:.2f} H={r.high:.2f} L={r.low:.2f} C={r.close:.2f} "
                  f"Vol={r.volume:,} Amt={r.amount/1e8:.2f}亿")
    except Exception as e:
        print(f"   错误: {e}")
