import React from 'react'
import {Redirect, Route, IndexRoute} from 'react-router'

import App from './views/app'

let routes = (
  <Route path="/dashboard" component={App}>
  </Route>
)

export default routes;
