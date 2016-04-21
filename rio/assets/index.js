import { createStore } from 'redux'
import rioReducer from './reducers/index'
import { render } from './components/index'

const store = createStore(rioReducer)

render()

store.subscribe(render)
