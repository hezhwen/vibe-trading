#!/usr/bin/env python3
"""
TDX .day 转 Parquet ETL 脚本

功能:
1. 将 TDX .day 不复权数据转换为 Parquet 格式（按年度分区）
2. 处理成交量单位（股→手）
3. 生成股票元数据（symbol_meta）

用法:
    python -m scripts.tdx.convert --vipdoc <vipdoc_path> --output <output_dir>
    python -m scripts.tdx.convert --vipdoc <vipdoc_path> --output <output_dir> --verify  # 仅验证模式

验证 (P0-0):
    python -m scripts.tdx.convert --vipdoc <vipdoc_path> --verify
    
    验证内容:
    1. 成交量单位验证（用平安银行/茅台对账）
    2. OHLC 关系校验（high >= low）
    3. 涨停价接近度检查

输出目录结构:
    data/
    ├── parquet/
    │   └── stock/{symbol}/{year}.parquet
    └── duckdb/
        └── china_a.duckdb
"""

import argparse
import sys
import struct
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional
import logging

import pandas as pd
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAS_PYARROW = True
except ImportError:
    HAS_PYARROW = False
    print("警告: pyarrow 未安装，将跳过 Parquet 写入")

from scripts.tdx.parser import TdxDayParser

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class VolumeConfig:
    """成交量配置"""
    unit: str = 'share'  # 'hand' or 'share'
    correction_factor: int = 100  # 除以100转换为手


@dataclass
class VerifyResult:
    """验证结果"""
    passed: bool
    checks: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def parse_day_record(data: bytes, volume_correction: int = 100) -> Optional[dict]:
    """解析单条 .day 记录
    
    Args:
        data: 32字节原始数据
        volume_correction: 成交量修正因子（100=股转手）
    """
    FORMAT = '<IIIIIfII'
    try:
        fields = struct.unpack(FORMAT, data)
    except struct.error:
        return None
    
    date = fields[0]
    if date < 19900000 or date > 21000000:
        return None
    
    return {
        'trade_date': date,
        'open': fields[1] / 100.0,
        'high': fields[2] / 100.0,
        'low': fields[3] / 100.0,
        'close': fields[4] / 100.0,
        'amount': fields[5],
        'volume': fields[6] // volume_correction,  # 修正为手
    }


def verify_volume_unit(parser: TdxDayParser, market: str, code: str) -> VerifyResult:
    """验证成交量单位
    
    验证方法: amount / (volume × 100 × close) ≈ 1.0
    """
    result = VerifyResult(passed=True)
    
    logger.info(f"验证 {market}{code} 成交量单位...")
    
    # 测试样本
    test_cases = [
        ('sz', '000001', '平安银行', 20230103, 12.85, 0.05),
        ('sh', '600519', '贵州茅台', 20230103, 1855.00, 0.05),
    ]
    
    for mkt, cod, name, target_date, expected_close, tolerance in test_cases:
        try:
            records = list(parser.parse(mkt, cod))
        except FileNotFoundError:
            result.warnings.append(f"{mkt}{cod} 文件不存在")
            continue
        
        if not records:
            result.warnings.append(f"{mkt}{cod} 无数据")
            continue
        
        # 找最接近目标日期的记录
        closest = min(records, key=lambda r: abs(r.date - target_date))
        
        # 计算 ratio
        if closest.close > 0 and closest.volume > 0:
            raw_ratio = closest.amount / (closest.volume * 100 * closest.close)
        else:
            raw_ratio = 0
        
        # 判断单位
        if 0.95 <= raw_ratio <= 1.05:
            volume_unit = 'hand'
        elif 0.0095 <= raw_ratio <= 0.0105:
            volume_unit = 'share (需÷100)'
        else:
            volume_unit = f'unknown (ratio={raw_ratio:.4f})'
            result.errors.append(f"{name}: ratio={raw_ratio:.4f}，异常")
        
        result.checks[f'{mkt}{cod}_{name}'] = {
            'date': closest.date,
            'close': closest.close,
            'volume': closest.volume,
            'amount': closest.amount,
            'ratio': raw_ratio,
            'volume_unit': volume_unit,
        }
        
        logger.info(f"  {name}: ratio={raw_ratio:.4f} → {volume_unit}")
    
    if result.errors:
        result.passed = False
    
    return result


def verify_ohlc(parser: TdxDayParser, market: str, code: str, sample_size: int = 1000) -> VerifyResult:
    """验证 OHLC 关系 (high >= low)"""
    result = VerifyResult(passed=True)
    
    try:
        records = list(parser.parse(market, code))
    except FileNotFoundError:
        return VerifyResult(passed=False, errors=[f"{market}{code} 文件不存在"])
    
    # 取最近 sample_size 条
    records = records[-sample_size:]
    
    violations = []
    for r in records:
        if r.high < r.low:
            violations.append({
                'date': r.date,
                'high': r.high,
                'low': r.low,
                'close': r.close,
            })
    
    result.checks['ohlc_violations'] = len(violations)
    if violations:
        result.warnings.append(f"发现 {len(violations)} 条 OHLC 违规记录（high < low）")
        result.checks['violation_samples'] = violations[:5]
    
    return result


def verify_limit_up(parser: TdxDayParser, market: str, code: str) -> VerifyResult:
    """验证涨停价接近度"""
    result = VerifyResult(passed=True)
    
    try:
        records = list(parser.parse(market, code))
    except FileNotFoundError:
        return VerifyResult(passed=False, errors=[f"{market}{code} 文件不存在"])
    
    # 检查涨停日成交额是否异常小
    limit_up_records = []
    for i, r in enumerate(records):
        if i == 0:
            continue
        
        prev = records[i-1]
        limit_pct = 0.1  # 假设主板
        
        # 判断是否涨停
        if abs(r.close - prev.close * (1 + limit_pct)) < 0.02:
            # 计算封板强度代理
            # 成交额/收盘价/100 = 成交量(手)
            if r.close > 0:
                est_volume_hand = r.amount / r.close / 100
                limit_up_records.append({
                    'date': r.date,
                    'close': r.close,
                    'prev_close': prev.close,
                    'amount': r.amount,
                    'est_vol_hand': est_volume_hand,
                })
    
    result.checks['limit_up_count'] = len(limit_up_records)
    return result


def run_verification(vipdoc_path: str) -> VerifyResult:
    """运行完整验证"""
    logger.info("="*60)
    logger.info("P0-0 数据质量验证")
    logger.info("="*60)
    
    parser = TdxDayParser(vipdoc_path)
    overall = VerifyResult(passed=True)
    
    # 1. 成交量单位验证
    logger.info("\n[1/3] 验证成交量单位...")
    vol_result = verify_volume_unit(parser, 'sz', '000001')
    vol_result2 = verify_volume_unit(parser, 'sh', '600519')
    overall.checks['volume_unit'] = {**vol_result.checks, **vol_result2.checks}
    if not vol_result.passed or not vol_result2.passed:
        overall.passed = False
        overall.errors.extend(vol_result.errors + vol_result2.errors)
    
    # 2. OHLC 关系验证
    logger.info("\n[2/3] 验证 OHLC 关系...")
    ohlc_result = verify_ohlc(parser, 'sz', '000001')
    overall.checks['ohlc'] = ohlc_result.checks
    if not ohlc_result.passed:
        overall.passed = False
    overall.warnings.extend(ohlc_result.warnings)
    
    # 3. 涨停价验证
    logger.info("\n[3/3] 验证涨停板数据...")
    limit_result = verify_limit_up(parser, 'sz', '000001')
    overall.checks['limit_up'] = limit_result.checks
    
    logger.info("\n" + "="*60)
    if overall.passed:
        logger.info("✅ 验证通过")
    else:
        logger.error("❌ 验证失败")
        for err in overall.errors:
            logger.error(f"  ERROR: {err}")
    for warn in overall.warnings:
        logger.warning(f"  WARNING: {warn}")
    
    return overall


def convert_day_to_parquet(
    vipdoc_path: str,
    output_path: str,
    markets: list = None,
    volume_correction: int = 100,
    year_start: int = 2000,
    year_end: int = 2030,
) -> dict:
    """转换 .day 文件为 Parquet 格式
    
    Args:
        vipdoc_path: TDX vipdoc 目录路径
        output_path: 输出目录路径
        markets: 市场列表 ['sh', 'sz', 'bj']，None 表示全部
        volume_correction: 成交量修正因子
        year_start: 起始年份
        year_end: 结束年份
    """
    if markets is None:
        markets = ['sh', 'sz', 'bj']
    
    parser = TdxDayParser(vipdoc_path)
    output_dir = Path(output_path)
    
    stats = {
        'processed': 0,
        'failed': 0,
        'skipped': 0,
        'files': {},
    }
    
    for market in markets:
        logger.info(f"\n处理市场: {market.upper()}")
        stock_list = parser.get_stock_list(market)
        logger.info(f"  股票数量: {len(stock_list)}")
        
        market_stats = {'success': 0, 'failed': 0, 'records': 0}
        
        for i, code in enumerate(stock_list):
            if (i + 1) % 500 == 0:
                logger.info(f"  进度: {i+1}/{len(stock_list)}")
            
            try:
                records = list(parser.parse(market, code))
                
                # 按年份分组
                yearly_data = {}
                for r in records:
                    year = r.date // 10000
                    if year < year_start or year > year_end:
                        continue
                    
                    # 修正成交量
                    record_dict = {
                        'trade_date': r.date,
                        'symbol': f'{code.upper()}.{market.upper()}',
                        'open': r.open,
                        'high': r.high,
                        'low': r.low,
                        'close': r.close,
                        'amount': r.amount,
                        'volume': r.volume // volume_correction,  # 修正为手
                    }
                    
                    if year not in yearly_data:
                        yearly_data[year] = []
                    yearly_data[year].append(record_dict)
                
                # 写入 Parquet
                for year, year_records in yearly_data.items():
                    symbol = f'{code.upper()}.{market.upper()}'
                    year_dir = output_dir / 'stock' / symbol
                    year_dir.mkdir(parents=True, exist_ok=True)
                    
                    parquet_file = year_dir / f'{year}.parquet'
                    df = pd.DataFrame(year_records)
                    
                    if HAS_PYARROW:
                        table = pa.Table.from_pandas(df)
                        pq.write_table(
                            table, 
                            parquet_file,
                            compression='zstd',
                            use_dictionary=True,
                        )
                    
                    market_stats['records'] += len(year_records)
                
                market_stats['success'] += 1
                stats['processed'] += 1
                
            except Exception as e:
                market_stats['failed'] += 1
                stats['failed'] += 1
                logger.warning(f"  失败 {market}{code}: {e}")
        
        stats['files'][market] = market_stats
        logger.info(f"  {market.upper()} 完成: 成功={market_stats['success']}, 失败={market_stats['failed']}, 记录={market_stats['records']}")
    
    return stats


def main():
    parser = argparse.ArgumentParser(description='TDX .day ETL 工具')
    parser.add_argument('--vipdoc', required=True, help='TDX vipdoc 目录路径')
    parser.add_argument('--output', default='./data/parquet', help='输出目录')
    parser.add_argument('--verify', action='store_true', help='仅运行验证')
    parser.add_argument('--markets', nargs='+', choices=['sh', 'sz', 'bj'], help='指定市场')
    parser.add_argument('--year-start', type=int, default=2000, help='起始年份')
    parser.add_argument('--year-end', type=int, default=2030, help='结束年份')
    
    args = parser.parse_args()
    
    # 验证 pyarrow
    if not HAS_PYARROW:
        logger.error("需要安装 pyarrow: pip install pyarrow")
        sys.exit(1)
    
    if args.verify:
        result = run_verification(args.vipdoc)
        sys.exit(0 if result.passed else 1)
    
    # 运行转换
    logger.info("="*60)
    logger.info("TDX .day → Parquet ETL")
    logger.info("="*60)
    logger.info(f"源目录: {args.vipdoc}")
    logger.info(f"输出目录: {args.output}")
    
    stats = convert_day_to_parquet(
        vipdoc_path=args.vipdoc,
        output_path=args.output,
        markets=args.markets,
        volume_correction=100,  # 股转手
        year_start=args.year_start,
        year_end=args.year_end,
    )
    
    logger.info("\n" + "="*60)
    logger.info("ETL 完成")
    logger.info(f"处理: {stats['processed']}, 失败: {stats['failed']}")
    for market, m_stats in stats['files'].items():
        logger.info(f"  {market.upper()}: {m_stats['success']} 文件, {m_stats['records']} 条记录")


if __name__ == '__main__':
    main()
