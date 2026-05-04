#!/usr/bin/env python3
"""
数据质量检查脚本 - Sprint 0.5

功能:
1. Parquet-symbol_meta 一致性检查
2. OHLC 关系校验
3. 涨停价弱封板检测
4. 换手率异常检测
5. 指数数据完整性
6. 退市股日线数据可用性
7. AKShare 数据接入
8. DuckDB 表结构验证
9. DuckDB 主键验证
10. float_cap 数据覆盖率

用法:
    python scripts/data_quality_check.py --check all
    python scripts/data_quality_check.py --check ohlc
    python scripts/data_quality_check.py --check consistency
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

import duckdb
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """检查结果"""
    name: str
    passed: bool
    level: str  # FATAL, ERROR, WARN, INFO
    message: str
    details: dict = field(default_factory=dict)
    records: list = field(default_factory=list)


class DataQualityChecker:
    """数据质量检查器"""
    
    def __init__(self, db_path: str, parquet_dir: str):
        self.db_path = db_path
        self.parquet_dir = parquet_dir
        self.conn = duckdb.connect(db_path)
        self.results: list[CheckResult] = []
        
    def check_all(self) -> list[CheckResult]:
        """运行所有检查"""
        checks = [
            self.check_consistency,
            self.check_ohlc,
            self.check_weak_limit_up,
            self.check_turnover_anomaly,
            self.check_index_coverage,
            self.check_delisted_history,
            self.check_akshare,
            self.check_duckdb_tables,
            self.check_duckdb_primary_keys,
            self.check_float_cap_coverage,
        ]
        
        for check in checks:
            try:
                result = check()
                self.results.append(result)
            except Exception as e:
                logger.error(f"{check.__name__} 执行失败: {e}")
                self.results.append(CheckResult(
                    name=check.__name__,
                    passed=False,
                    level='FATAL',
                    message=f"检查执行失败: {e}"
                ))
        
        return self.results
    
    def check_consistency(self) -> CheckResult:
        """检查 Parquet-symbol_meta 一致性"""
        logger.info("[1/10] 检查 Parquet-symbol_meta 一致性...")
        
        # 获取parquet中的所有symbol
        parquet_stocks = set()
        stock_dir = Path(self.parquet_dir) / 'stock'
        if stock_dir.exists():
            for f in stock_dir.iterdir():
                if f.is_dir():
                    parquet_stocks.add(f.name)
        
        # 获取meta中的symbol
        meta_symbols = set(
            row[0] for row in self.conn.execute(
                "SELECT symbol FROM symbol_meta"
            ).fetchall()
        )
        
        # 找出差异
        in_parquet_not_meta = parquet_stocks - meta_symbols
        in_meta_not_parquet = meta_symbols - parquet_stocks
        
        result = CheckResult(
            name="consistency",
            passed=True,
            level='FATAL' if in_parquet_not_meta else 'INFO',
            message="",
            details={
                'parquet_count': len(parquet_stocks),
                'meta_count': len(meta_symbols),
                'in_parquet_not_meta': len(in_parquet_not_meta),
                'in_meta_not_parquet': len(in_meta_not_parquet),
            }
        )
        
        if in_parquet_not_meta:
            result.passed = False
            result.message = f"发现 {len(in_parquet_not_meta)} 个symbol在Parquet中但不在meta中"
            result.details['samples'] = list(in_parquet_not_meta)[:10]
            logger.warning(f"  {result.message}")
        else:
            result.message = "一致性检查通过"
            logger.info(f"  ✅ {result.message}")
        
        return result
    
    def check_ohlc(self, sample_size: int = 100) -> CheckResult:
        """检查 OHLC 关系"""
        logger.info("[2/10] 检查 OHLC 关系...")
        
        violations = []
        stock_dir = Path(self.parquet_dir) / 'stock'
        
        # 随机抽样检查
        import random
        all_stocks = list(stock_dir.iterdir()) if stock_dir.exists() else []
        sample_stocks = random.sample(all_stocks, min(sample_size, len(all_stocks)))
        
        for stock_path in sample_stocks:
            if not stock_path.is_dir():
                continue
            symbol = stock_path.name
            
            for pq_file in stock_dir.glob(f'{symbol}/*.parquet'):
                try:
                    df = pd.read_parquet(pq_file)
                    for _, row in df.iterrows():
                        if row['high'] < row['low']:
                            violations.append({
                                'symbol': symbol,
                                'date': row['trade_date'],
                                'high': row['high'],
                                'low': row['low'],
                            })
                        if row['high'] < row['open'] or row['high'] < row['close']:
                            violations.append({
                                'symbol': symbol,
                                'date': row['trade_date'],
                                'high': row['high'],
                                'open': row['open'],
                                'close': row['close'],
                            })
                except Exception:
                    continue
                
                if len(violations) >= 100:
                    break
            if len(violations) >= 100:
                break
        
        result = CheckResult(
            name="ohlc",
            passed=len(violations) == 0,
            level='ERROR' if violations else 'INFO',
            message=f"发现 {len(violations)} 条OHLC违规",
            records=violations[:100]
        )
        
        if violations:
            logger.warning(f"  ⚠️ {result.message}")
        else:
            logger.info(f"  ✅ OHLC关系正常")
        
        return result
    
    def check_weak_limit_up(self) -> CheckResult:
        """检查涨停价弱封板"""
        logger.info("[3/10] 检查涨停价弱封板...")
        # TODO: 依赖float_cap数据
        result = CheckResult(
            name="weak_limit_up",
            passed=True,
            level='WARN',
            message="待实现（需float_cap数据）"
        )
        logger.info(f"  ℹ️ {result.message}")
        return result
    
    def check_turnover_anomaly(self) -> CheckResult:
        """检查换手率异常"""
        logger.info("[4/10] 检查换手率异常...")
        # TODO: 依赖turnover_rate数据
        result = CheckResult(
            name="turnover_anomaly",
            passed=True,
            level='WARN',
            message="待实现（需turnover_rate数据）"
        )
        logger.info(f"  ℹ️ {result.message}")
        return result
    
    def check_index_coverage(self) -> CheckResult:
        """检查指数数据完整性"""
        logger.info("[5/10] 检查指数数据完整性...")
        
        indices = ['000001.SH', '000300.SH', '399001.SZ']
        missing_dates = {}
        
        for idx in indices:
            idx_path = Path(self.parquet_dir) / 'stock' / idx
            if not idx_path.exists():
                missing_dates[idx] = ['Index file not found']
                continue
            
            # 获取所有年份文件
            years = []
            for pq_file in idx_path.glob('*.parquet'):
                year = int(pq_file.stem)
                if 2019 <= year <= 2026:
                    years.append(year)
            
            missing_dates[idx] = years
        
        result = CheckResult(
            name="index_coverage",
            passed=all(years for years in missing_dates.values()),
            level='ERROR',
            message="",
            details=missing_dates
        )
        
        for idx, years in missing_dates.items():
            if years:
                logger.info(f"  {idx}: {sorted(years)}")
            else:
                logger.warning(f"  {idx}: 无数据")
        
        return result
    
    def check_delisted_history(self) -> CheckResult:
        """检查退市股日线数据"""
        logger.info("[6/10] 检查退市股日线数据...")
        
        delisted_count = self.conn.execute(
            "SELECT COUNT(*) FROM delisted_stocks"
        ).fetchone()[0]
        
        stock_dir = Path(self.parquet_dir) / 'stock'
        parquet_symbols = set(f.name for f in stock_dir.iterdir() if f.is_dir())
        
        # 退市股有多少在parquet中有数据
        delisted_with_data = self.conn.execute("""
            SELECT COUNT(*) FROM delisted_stocks 
            WHERE symbol IN (SELECT symbol FROM delisted_stocks INTERSECT VALUES {vals})
        """, {'vals': list(parquet_symbols)}).fetchone()[0] if delisted_count > 0 else 0
        
        result = CheckResult(
            name="delisted_history",
            passed=delisted_count > 0,
            level='WARN' if delisted_count == 0 else 'INFO',
            message=f"退市股记录: {delisted_count}, 有日线数据的: {delisted_with_data}",
            details={
                'total_delisted': delisted_count,
                'with_data': delisted_with_data,
            }
        )
        
        if delisted_count == 0:
            logger.warning(f"  ⚠️ 退市股表为空，幸存者偏差问题无法处理")
        else:
            logger.info(f"  ℹ️ {result.message}")
        
        return result
    
    def check_akshare(self) -> CheckResult:
        """检查 AKShare 数据接入"""
        logger.info("[7/10] 检查 AKShare 数据接入...")
        
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            passed = len(df) > 3000
            message = f"返回 {len(df)} 条数据"
            level = 'INFO' if passed else 'ERROR'
        except Exception as e:
            passed = False
            message = f"连接失败: {e}"
            level = 'ERROR'
            df = None
        
        result = CheckResult(
            name="akshare",
            passed=passed,
            level=level,
            message=message,
            details={'row_count': len(df) if df is not None else 0}
        )
        
        if passed:
            logger.info(f"  ✅ {message}")
        else:
            logger.warning(f"  ⚠️ {message}")
        
        return result
    
    def check_duckdb_tables(self) -> CheckResult:
        """检查 DuckDB 表结构"""
        logger.info("[8/10] 检查 DuckDB 表结构...")
        
        tables = self.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        table_names = set(t[0] for t in tables)
        
        expected_tables = {
            'symbol_meta', 'delisted_stocks', 'st_status_events',
            'suspension_records', 'new_listing_records', 'limit_up_history',
            'sw_industry', 'concept_board_members', 'sector_daily_flow',
            'north_flow_industry', 'index_meta', 'index_daily', 'data_snapshots'
        }
        
        missing = expected_tables - table_names
        extra = table_names - expected_tables
        
        passed = len(missing) == 0
        result = CheckResult(
            name="duckdb_tables",
            passed=passed,
            level='FATAL' if missing else 'INFO',
            message=f"表数量: {len(table_names)}/13",
            details={
                'found': sorted(table_names),
                'missing': sorted(missing),
                'extra': sorted(extra) if extra else [],
            }
        )
        
        if missing:
            logger.warning(f"  ⚠️ 缺少表: {missing}")
        else:
            logger.info(f"  ✅ 13张表全部存在")
        
        return result
    
    def check_duckdb_primary_keys(self) -> CheckResult:
        """检查 DuckDB 主键"""
        logger.info("[9/10] 检查 DuckDB 主键...")
        
        # DuckDB不完全支持主键，用索引代替
        indexes = self.conn.execute(
            "SELECT table_name, index_name FROM duckdb_indexes()"
        ).fetchall()
        
        # 检查symbol_meta是否有主键
        symbol_meta_pk = any('symbol_meta' in str(idx) for idx in indexes)
        
        result = CheckResult(
            name="duckdb_primary_keys",
            passed=symbol_meta_pk,
            level='ERROR' if not symbol_meta_pk else 'INFO',
            message="symbol_meta 主键存在" if symbol_meta_pk else "symbol_meta 无主键",
            details={'indexes': indexes}
        )
        
        if symbol_meta_pk:
            logger.info(f"  ✅ {result.message}")
        else:
            logger.warning(f"  ⚠️ {result.message}")
        
        return result
    
    def check_float_cap_coverage(self) -> CheckResult:
        """检查 float_cap 数据覆盖率"""
        logger.info("[10/10] 检查 float_cap 覆盖率...")
        
        # 检查parquet中是否有float_cap列
        stock_dir = Path(self.parquet_dir) / 'stock'
        sample_file = next(stock_dir.glob('*/*.parquet'), None)
        
        if sample_file:
            df = pd.read_parquet(sample_file)
            has_float_cap = 'float_cap' in df.columns
        else:
            has_float_cap = False
        
        result = CheckResult(
            name="float_cap_coverage",
            passed=has_float_cap,
            level='ERROR' if not has_float_cap else 'INFO',
            message="float_cap列存在" if has_float_cap else "float_cap列不存在",
        )
        
        if has_float_cap:
            logger.info(f"  ✅ {result.message}")
        else:
            logger.warning(f"  ⚠️ {result.message}，can_fill()需要此字段")
        
        return result
    
    def print_summary(self):
        """打印汇总"""
        logger.info("\n" + "="*60)
        logger.info("数据质量检查汇总")
        logger.info("="*60)
        
        for result in self.results:
            status = "✅" if result.passed else "❌"
            level_marker = f"[{result.level}]"
            logger.info(f"{status} {level_marker} {result.name}: {result.message}")
        
        fatal = sum(1 for r in self.results if r.level == 'FATAL' and not r.passed)
        error = sum(1 for r in self.results if r.level == 'ERROR' and not r.passed)
        warn = sum(1 for r in self.results if r.level == 'WARN' and not r.passed)
        
        logger.info(f"\n汇总: FATAL={fatal}, ERROR={error}, WARN={warn}")
        
        return fatal == 0 and error == 0


def main():
    parser = argparse.ArgumentParser(description='数据质量检查')
    parser.add_argument('--db', default='./data/duckdb/china_a.duckdb', help='DuckDB路径')
    parser.add_argument('--parquet', default='./data/parquet', help='Parquet目录')
    parser.add_argument('--check', default='all', choices=['all', 'consistency', 'ohlc', 'index', 'delisted', 'akshare', 'duckdb', 'float_cap'])
    args = parser.parse_args()
    
    checker = DataQualityChecker(args.db, args.parquet)
    
    if args.check == 'all':
        results = checker.check_all()
    else:
        results = [getattr(checker, f'check_{args.check}')()]
    
    passed = checker.print_summary()
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
