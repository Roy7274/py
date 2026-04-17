import {useState, useEffect} from 'react';
import TransactionList from './components/TransactionList';
import TransactionForm from './components/TransactionForm';
import Statistics from './components/Statistics';
import './App.css'
function App() {
  const[transactions, setTransactions ] = useState([]);
  const[statistics, setStatistics] = useState({
    total_income: 0,total_expense: 0, balance: 0, category_summary:{}
  })
  const fetchAlldata = async () =>{
    fetch('http://localhost:8000/api/transactions')
    .then(res => res.json())
    .then(data => {
      console.log("backend data:", data);
      setTransactions(data);
    });

    const statsRes = await fetch('http://localhost:8000/api/statistics');
    setStatistics(await statsRes.json());
  }

  useEffect( () => {
    fetchAlldata();
  },[]);

  const handleAddTransaction = async (newData) => {
    const res = await fetch('http://localhost:8000/api/transactions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(newData)
    })
     if (res.ok) {
      fetchAlldata(); // 复用上面的函数
    } else {
      alert('添加失败，请看后端日志');
    }

  }

  const handleDeleteTransaction = async (id) => {
    await fetch(`http://localhost:8000/api/transactions/${id}` , { method: 'DELETE' });
    fetchAlldata(); 
  };

  return (
        <div className="app">
      <header className="app-header">
        <h1>💰 Expense Tracker</h1>
      </header>

      <div className="container">
        <TransactionForm onAdd={handleAddTransaction} />
        <Statistics statistics={statistics} />
        {/* 🆕 把删除函数传给列表 */}
        <TransactionList 
          transactions={transactions} 
          onDelete={handleDeleteTransaction} 
        />
      </div>
    </div>
  );

}

export default App;
