import React from 'react'
import ReactDOM from 'react-dom'
import { Router, Route, browserHistory } from 'react-router'
import { syncHistoryWithStore } from 'react-router-redux'
import { Provider } from 'react-redux'
import Store from '../store/index'

import App from './app'

const history = syncHistoryWithStore(browserHistory, Store)

export const render = function () {
  ReactDOM.render(
    <Provider store={Store}>
      <Router history={history}>
        <Route path="/dashboard" component={App}>
        </Route>
      </Router>
    </Provider>,
    document.getElementById('root')
  )
}
