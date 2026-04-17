import {useState, useEffect} from 'react';
import TransactionList from './components/TransactionList';
import TransactionForm from './components/TransactionForm';
function App() {
  const[transactions, setTransactions ] = useState([]);
  const fetchTransactions = () =>{
    fetch('http://localhost:8000/api/transactions')
    .then(res => res.json())
    .then(data => {
      console.log("backend data:", data);
      setTransactions(data);
    });
  }

  useEffect( () => {
    fetchTransactions();
  },[]);

  const handleAddTransaction = async (newData) => {
    const res = await fetch('http://localhost:8000/api/transactions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(newData)
    })
     if (res.ok) {
      fetchTransactions(); // 复用上面的函数
    } else {
      alert('添加失败，请看后端日志');
    }
  }
  return (
    <div style={{ padding: '20px'}}>
      <h1>Expense Tracker</h1>
      <TransactionForm onAdd={handleAddTransaction} />
      <TransactionList transactions={transactions} />
    </div>
  );

}

export default App;
