import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import '@carbon/styles/css/styles.css'
import '@xyflow/react/dist/style.css'
import './styles.css'
import App from './App'
import { basePath } from './runtime'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><BrowserRouter basename={basePath || undefined}><App /></BrowserRouter></React.StrictMode>,
)
