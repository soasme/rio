import React from 'react'
import ReactDOM from 'react-dom'
import { Router, Route, browserHistory } from 'react-router'
import { syncHistoryWithStore } from 'react-router-redux'
import { Provider } from 'react-redux'
import Store from './store/index'
import routes from './routes'

const history = syncHistoryWithStore(browserHistory, Store)

const render = function () {
  ReactDOM.render(
    <Provider store={Store}>
      <Router history={history}>
      { routes }
      </Router>
    </Provider>,
    document.getElementById('root')
  )
}

render()
Store.subscribe(render)
