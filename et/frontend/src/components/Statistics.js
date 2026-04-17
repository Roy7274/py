
function Statistics({statistics}){
    const { total_income, total_expense, balance, category_summary } = statistics;

    return(
    <div className="card">
        <h2>📊 统计报表</h2>

        {/* 上方三个数字盒子 */}
        <div className="stats-overview">
            <div className="stat-box income">
            <div className="stat-label">💰 总收入</div>
            <div className="stat-value">¥{total_income.toFixed(1)}</div>
            </div>
            <div className="stat-box expense">
            <div className="stat-label">💸 总支出</div>
            <div className="stat-value">¥{total_expense.toFixed(1)}</div>
            </div>
            <div className="stat-box balance">
            <div className="stat-label">📊 结余</div>
            <div className="stat-value">¥{balance.toFixed(1)}</div>
            </div>
        </div>

        {Object.keys(category_summary).length > 0 && (
        <div className="category-stats">
          <h3>🏷️ 支出分类明细</h3>
          {Object.entries(category_summary).map(([category, amount]) => (
            <div key={category} className="category-row">
              <span className="category-name">{category}</span>
              <div className="bar-container">
                {/* 动态计算宽度百分比，实现条形图效果 */}
                <div
                  className="bar"
                  style={{ width: `${(amount / total_expense) * 100}%` }}
                />
              </div>
              <span className="category-amount">¥{amount.toFixed(1)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
    )
}

export default Statistics;