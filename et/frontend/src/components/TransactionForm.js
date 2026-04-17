// frontend/src/components/TransactionForm.js
import { useState } from 'react';

function TransactionForm({ onAdd }) {
  const [type, setType] = useState('支出');
  const [amount, setAmount] = useState('');
  const [category, setCategory] = useState('');
  const [note, setNote] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!amount || !category) return;

    onAdd({
      type: type,
      amount: parseFloat(amount),
      category: category,
      note: note
    });

    setAmount('');
    setCategory('');
    setNote('');
  };

  return (
    <div className="card">
      <h2>📝 添加记录</h2>
      <form onSubmit={handleSubmit}>
        
        <div className="type-toggle">
          <button
            type="button"
            className={type === '支出' ? 'active expense' : ''}
            onClick={() => setType('支出')}
          >
            📤 支出
          </button>
          <button
            type="button"
            className={type === '收入' ? 'active income' : ''}
            onClick={() => setType('收入')}
          >
            📥 收入
          </button>
        </div>

        <div className="form-row">
          <input
            type="number"
            placeholder="金额"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            min="0.01"
            step="0.01"
          />
          <input
            type="text"
            placeholder="分类（如：餐饮、交通）"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          />
        </div>

        <input
          type="text"
          placeholder="备注（选填）"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          style={{ marginBottom: '12px' }} // 唯一一个没写在 App.css 里的微调
        />

        <button type="submit" className="btn-submit">
          ✅ 添加{type}
        </button>
      </form>
    </div>
  );
}

export default TransactionForm;
